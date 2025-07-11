# app/emo_engine/persona/states.py

from __future__ import annotations

import asyncio, time, math, uuid, logging

from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt, atan2, pi
from typing import Dict, List

from app.config import settings
from .utils.trigger_patterns import apply_triggers

from .memory import MemoryEntry, _persist
from .constants.temperaments import TEMPERAMENT_PROFILE
from .constants.zodiacs import ZODIAC_MODIFIERS

from .utils.emotion_math import(
    suppress_opposite, _compute_tertiary, _compute_secondary,
    _clamp, EMO_DT_BASE, EMO_DT_DEFAULT,
)
from .constants.emotions import (
    ALL_METRICS, PRIMARY_EMOTIONS, ANALYSIS_METRICS, FAT_CLAMP,
)


def _recompute_rates(self) -> None:
    total = sum(self.temperament.values()) or 1.0
    self.temperament = {k: v / total for k, v in self.temperament.items()}
    base = {m: 1.0 for m in ALL_METRICS}
    for t, w in self.temperament.items():
        prof = TEMPERAMENT_PROFILE[t]
        for m in ALL_METRICS:
            base[m] *= (prof.get(m, 1.0) ** w)

    zmod = ZODIAC_MODIFIERS.get(self.zodiac, {})
    self.change_rates = {m: base[m] * zmod.get(m, 1.0) for m in ALL_METRICS}


def _compute_salience(self, readings: Dict[str, float], text: str) -> float:
    numer = (
        abs(readings.get("valence", 0.0))
        + sum(abs(readings[e] - 0.5) for e in PRIMARY_EMOTIONS if e != "valence")
    )
    base = (numer / (len(PRIMARY_EMOTIONS) + 1)) / max(1.0, len(text) ** 0.5)
    bonus = (
        text.count("!") * 0.02
      + text.count("?") * 0.01
      + sum(text.count(e) for e in ("😀","😂","😢","😡","😱","👍","👎","❤️")) * 0.01
    )
    salience = min(1.0, base + min(0.15, bonus))
    return max(settings.MEMORY_MIN_SALIENCE, salience)


def _decayed_weight(self, uid: int) -> float:
    w, ts = self.user_weights.get(uid, [0.0, time.time()])
    if settings.PERSONA_WEIGHT_HALFLIFE <= 0:
        return w
    return max(0.0, w * 0.5 ** ((time.time() - ts) / settings.PERSONA_WEIGHT_HALFLIFE))


def _update_weight(self, uid: int) -> float:
    new_w = min(1.0, self._decayed_weight(uid) + settings.PERSONA_WEIGHT_STEP)
    self.user_weights[uid] = [new_w, time.time()]
    return new_w


def _blend_metric(self, metric: str, delta: float, weight: float) -> None:
        
    base_rate = self.change_rates.get(metric, 1.0)
    base_blend = settings.PERSONA_BLEND_FACTOR
    dynamic_blend = base_blend * (1 - self.state["fatigue"])
    influence = dynamic_blend * weight * (1.5 if metric in PRIMARY_EMOTIONS else 1.0)
    influence = min(influence, 1.0)
        
    if abs(delta - self.state[metric]) > 0.8:
        influence *= 1.5
        
    raw_val = self.state[metric] * (1 - influence) + delta * base_rate * influence

    cr = self.change_rates.get(metric, 1.0)
        
    if cr == 0: cr = 1e-3
    alpha_dyn = settings.STATE_EMA_ALPHA / cr
        
    if abs(delta - self.state[metric]) > 0.33:
        alpha_dyn = settings.STATE_EMA_MAX_ALPHA
    alpha_dyn = min(settings.STATE_EMA_MAX_ALPHA, alpha_dyn)

    new_val = self.state[metric] * (1 - alpha_dyn) + raw_val * alpha_dyn
    lo, hi = (-1.0, 1.0) if metric == "valence" else (0.0, 1.0)

    if metric == "valence":
        new_val = max(-1.0, min(1.0, new_val))
    elif metric == "arousal":
        new_val = max(0.0, min(1.0, new_val))

    if metric == "valence":
        now = time.time()
        hold = now - getattr(self, "_last_valence_peak_ts", now)
        if abs(new_val) > 0.85:
            factor = min(1.0, hold / 60.0)
            new_val -= factor * 0.05 * (1 if new_val > 0 else -1)
        if abs(new_val) > 0.85 and getattr(self, "_in_peak", False) is False:
            self._last_valence_peak_ts = now
            self._in_peak = True
        elif abs(new_val) <= 0.85:
            self._in_peak = False

    self.state[metric] = max(lo, min(hi, new_val))
    suppress_opposite(metric, self.state)


def _update_mood_label(self) -> None:
    
    x = self.state["valence"]
    y = self.state["arousal"] * 2 - 1
    theta = atan2(y, x) % (2 * pi)

    prims = list(PRIMARY_EMOTIONS)
    idx   = round(theta / (2 * pi) * len(prims)) % len(prims)
    base  = prims[idx]
    r    = sqrt(x*x + y*y)
    
    if r > 0.66:
        strength = "strong"
    elif r > 0.33:
        strength = "moderate"
    else:
        strength = "weak"

    self.mood = f"{strength}_{base}"


async def process_interaction(self, uid: int, text: str) -> None:

    await self._restored_evt.wait()

    if self._loop_id != id(asyncio.get_running_loop()):
        self._lock = asyncio.Lock()
        self._loop_id = id(asyncio.get_running_loop())
        self._mods_cache = {}

    now = time.time()
    idle = now - getattr(self, "_last_ts", now)
    self._last_ts = now

    self.state["arousal"] *= pow(0.97, idle / 60)
    self.state["energy"]  *= pow(0.98, idle / 60)

    self.state["fatigue"] *= pow(settings.FATIGUE_RECOVERY_RATE, idle / 60)

    val_homeo = pow(settings.VALENCE_HOMEOSTASIS_DECAY, idle / 60)
    self.state["valence"] *= val_homeo

    decay_neg = pow(settings.EMO_PASSIVE_DECAY, idle / 60)
    for emo in ("stress", "anxiety", "anger"):
        self.state[emo] *= decay_neg

    try:
        local_tz = ZoneInfo(getattr(settings, "DEFAULT_TZ", "UTC"))
        now_loc  = datetime.now(local_tz)
        tz_hour  = now_loc.hour + now_loc.minute / 60
    except Exception:
        tz_hour  = (time.time() / 3600) % 24                         # fallback
    circadian = settings.CIRCADIAN_AMPLITUDE * math.sin((tz_hour - 3) / 24 * 2 * pi)
    self.state["arousal"] = self._clamp(self.state["arousal"] + circadian)
    idle = now - getattr(self, "_last_mood_change_ts", now)
    if idle > 300:
        self.state["valence"] *= settings.VALENCE_HOMEOSTASIS_DECAY ** (idle/60) * 1.2
    if self.mood != getattr(self, "_prev_mood", None):
        self._last_mood_change_ts = now
        self._prev_mood = self.mood

    self._last_uid = uid

    self._last_user_msg = text
    readings = await self.analyze_text(text)
    trust = readings.get("trust", 0.5)
    sigma = (1.0 - trust) * 0.05
    for m in ANALYSIS_METRICS:
        readings[m] = self._clamp(readings[m] + self._rng.gauss(0.0, sigma))

    readings["energy"] = self._clamp(
        len(text) / 200 + text.count("!") * 0.02 + text.count("?") * 0.01
    )

    if readings["arousal"] > settings.FATIGUE_AROUSAL_THRESHOLD \
       or readings["energy"] > settings.FATIGUE_ENERGY_THRESHOLD:
        delta_f = settings.FATIGUE_ACCUMULATE_RATE * \
                  (readings["arousal"] + readings["energy"]) / 2
        self.state["fatigue"] = FAT_CLAMP(self.state["fatigue"] + delta_f)

    async with self._lock:
        
        weight = self._update_weight(uid)

        from .utils import emotion_math as emo_math
        emo_math._init_matrices()
        A = emo_math.EMO_MATRIX_A
        B = emo_math.EMO_MATRIX_B

        s   = [self.state[m]             for m in ALL_METRICS]
        r   = [readings.get(m, 0.0)      for m in ALL_METRICS]
        diff = [r_j - s_j for s_j, r_j in zip(s, r)]

        N = len(s)
        for i, m in enumerate(ALL_METRICS):
            base = EMO_DT_BASE.get(m, EMO_DT_DEFAULT)
            dt = base * (1 - s[i])
            dotA = sum(A[i][j] * s[j] for j in range(N))
            dotB = sum(B[i][j] * diff[j] for j in range(N))
            self.state[m] = self._clamp(s[i] + dt * (dotA + dotB))

        idle_total = time.time() - self._last_mood_change_ts
        if idle_total > 300:
            self.state["valence"] *= settings.VALENCE_HOMEOSTASIS_DECAY ** (idle_total/60) * 1.2

        for metric in ALL_METRICS:
            suppress_opposite(metric, self.state)

        raw_deltas = apply_triggers(text)
        trigger_deltas = {
            metric: delta
            for metric, delta in raw_deltas.items()
            if metric in ALL_METRICS
        }

        for metric, delta in trigger_deltas.items():
            self._blend_metric(metric, self.state[metric] + delta, weight)

        self._update_mood_label()
        self._compute_secondary()
        self._compute_tertiary()


        sal = self._compute_salience(readings, text)
        imp = sal
        factor_imp = settings.APPRAISAL_IMPORTANCE_FACTOR * (1 - self.state["fatigue"])
        self.state["arousal"] = self._clamp(
            self.state["arousal"] + factor_imp * sal
        )
        expc = readings.get("anticipation", 0.5)
        surprise_delta = (1 - expc) * settings.APPRAISAL_EXPECTATION_FACTOR * (1 - self.state["fatigue"])
        self.state["surprise"] = self._clamp(
            self.state["surprise"] + surprise_delta
        )
        ctrl = readings.get("trust", 0.5) - readings.get("fear", 0.5)
        self.state["dominance"] = self._clamp(
            self.state.get("dominance", 0.5)
            + settings.APPRAISAL_CONTROL_FACTOR * ctrl
        )
        if readings.get("fear",0.0) > 0.8:
            drop = (readings["fear"] - 0.8)**2
            self.state["dominance"] *= (1 - drop)

        base_alpha = settings.EMO_EMA_ALPHA
        for e in PRIMARY_EMOTIONS + ["arousal", "energy", "fatigue"]:
            delta = abs(readings[e] - self.ema[e])
            alpha_dyn = max(0.05, min(0.30, base_alpha * (1 + 2 * delta)))
            self.ema[e] = self.ema[e] * (1 - alpha_dyn) + readings[e] * alpha_dyn

        self.ema["arousal"] = self._clamp(
            self.ema["arousal"] + (text.count("!") * 0.02 + text.count("?") * 0.01)
        )

        em_list = [(e, self.ema[e]) for e in PRIMARY_EMOTIONS]
        mean_  = sum(v for _, v in em_list) / len(em_list)
        std_   = (sum((v - mean_)**2 for _, v in em_list) / len(em_list))**0.5

        sigma_k = 0.5 + std_
        hysteresis = settings.EMO_HYSTERESIS_DELTA * (1 + std_)

        thr_on  = mean_ + sigma_k * std_ + hysteresis
        thr_off = mean_ + sigma_k * std_ - hysteresis

        top_em, top_val = max(em_list, key=lambda kv: kv[1])

        if not self.dominant_locked and top_val >= thr_on:
            self.dominant_locked = True
            self.current_dominant = top_em
        elif self.dominant_locked and top_val < thr_off:
            self.dominant_locked = False
            self.current_dominant = None

        if self.dominant_locked and self.current_dominant:
            sorted_em = sorted(em_list, key=lambda kv: kv[1], reverse=True)
            self.last_user_emotions = [
                self._EMO_LABEL_MAP[sorted_em[0][0]],
                self._EMO_LABEL_MAP[sorted_em[1][0]],
            ]
        else:
            self.last_user_emotions = []

        salience = self._compute_salience(readings, text)
        self.memory_entries.append(
            MemoryEntry(
                id=str(uuid.uuid4()),
                snippet=self._safe_snippet(text),
                timestamp=time.time(),
                salience=salience,
                readings={e: readings.get(e, 0.0) for e in PRIMARY_EMOTIONS},
            )
        )

        if len(self.memory_entries) > settings.MEMORY_MAX_ENTRIES:
            dropped = self.memory_entries.popleft()
            logging.debug("Memory capped → dropped entry %s", dropped.id)

        await self._persist()
        self._cached_style_modifiers = self.style_modifiers()