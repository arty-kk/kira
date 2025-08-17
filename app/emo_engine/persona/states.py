cat >app/emo_engine/persona/states.py<< EOF
#app/emo_engine/persona/states.py
from __future__ import annotations

import asyncio
import time
import math
import logging

from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt, atan2, pi
from typing import Dict, List
from collections import Counter

from app.config import settings
from app.core.memory import load_context
from .executor import EXECUTOR
from .memory import get_embedding
from .utils.trigger_patterns import apply_triggers
from .constants.temperaments import TEMPERAMENT_PROFILE
from .constants.zodiacs import ZODIAC_MODIFIERS
from .utils.emotion_math import(
    suppress_opposite, _compute_tertiary, _compute_secondary,
    _clamp, EMO_DT_BASE, EMO_DT_DEFAULT,
)
from .utils.emotion_math import EMO_MATRIX_A as A, EMO_MATRIX_B as B
from .constants.emotions import (
    ALL_METRICS, PRIMARY_EMOTIONS, ANALYSIS_METRICS, FAT_CLAMP,
)


logger = logging.getLogger(__name__)


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
    if len(self.user_weights) > 2000:
        oldest = sorted(self.user_weights.items(), key=lambda kv: kv[1][1])[:200]
        for k, _ in oldest:
            self.user_weights.pop(k, None)
    return new_w


def _blend_metric(self, metric: str, delta: float, weight: float) -> None:
        
    if 'valence' not in self.state:
        logger.error("Key 'valence' missing in persona.state at _blend_metric: %r", self.state)
        self.state['valence'] = 0.0

    base_rate = self.change_rates.get(metric, 1.0)
    base_blend = settings.PERSONA_BLEND_FACTOR
    dynamic_blend = base_blend * (1 - self.state.get("fatigue", 0.0))
    influence = dynamic_blend * weight * (1.5 if metric in PRIMARY_EMOTIONS else 1.0)
    influence = min(influence, 1.0)
        
    if abs(delta - self.state.get(metric, 0.0)) > 0.8:
        influence *= 1.5
        
    raw_val = self.state.get(metric, 0.0) * (1 - influence) + delta * base_rate * influence

    cr = self.change_rates.get(metric, 1.0) or 1e-3
        
    if cr == 0: cr = 1e-3
    alpha_dyn = settings.STATE_EMA_ALPHA / cr
        
    if abs(delta - self.state.get(metric, 0.0)) > 0.33:
        alpha_dyn = settings.STATE_EMA_MAX_ALPHA
    alpha_dyn = min(settings.STATE_EMA_MAX_ALPHA, alpha_dyn)

    new_val = self.state.get(metric, 0.0) * (1 - alpha_dyn) + raw_val * alpha_dyn
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
    self._dirty_metrics.add(metric)
    suppress_opposite(metric, self.state)


def _update_mood_label(self) -> None:
    
    x = self.state.get("valence", 0.0)
    y = self.state.get("arousal", 0.5) * 2 - 1
    theta = atan2(y, x) % (2 * pi)

    prims = list(PRIMARY_EMOTIONS)
    idx   = round(theta / (2 * pi) * len(prims)) % len(prims)
    base  = prims[idx]
    r    = sqrt(x*x + y*y)
    
    if r > 0.75:
        strength = "strong"
    elif r > 0.5:
        strength = "moderate"
    else:
        strength = "weak"

    self.mood = f"{strength}_{base}"


async def process_interaction(
    self,
    uid: int,
    text: str,
    user_gender: str | None = None,
) -> None:

    async with self._proc_sem:
        local_gender = user_gender if user_gender is not None else self.user_gender
        self.user_gender = local_gender

        defaults = {
            "valence": 0.0,
            "arousal": 0.5,
            "energy":  0.0,
            "stress":  0.0,
            "anxiety": 0.0,
        }
        for k in ALL_METRICS:
            self.state.setdefault(k, defaults.get(k, 0.0))

        curr = id(asyncio.get_running_loop())
        if self._loop_id != curr:
            self._loop_id = curr
            self._mods_cache.clear()

        now = time.time()
        punc = Counter(text)
        excl = punc["!"]
        ques = punc["?"]

        readings = await self._text_analyzer.analyze_text(text)
        hist_task = asyncio.create_task(load_context(self.chat_id, uid))
        try:
            raw_hist = await asyncio.wait_for(hist_task, timeout=1.5)
        except asyncio.TimeoutError:
            hist_task.cancel()
            raw_hist = []
        except Exception:
            logger.warning("load_context failed", exc_info=True)
            raw_hist = []

        ROLE = {"user": "USER", "assistant": "ASSISTANT"}
        ctx_lines: list[str] = []
        cur_snippet = self._safe_snippet(text)
        now_tag = time.strftime("%H:%M", time.localtime())
        ctx_lines.append(f"USER[{now_tag}]: {cur_snippet}")
        for m in reversed(raw_hist):
            if len(ctx_lines) >= 10:
                break
            r = m.get("role")
            if r not in ROLE:
                continue
            if r == "user" and m.get("user_id") != uid:
                continue
            snippet_raw = m.get("content", "")
            snippet = self._safe_snippet(snippet_raw)
            if snippet:
                ctx_lines.append(f"{ROLE[r]}: {snippet}")

        ctx_lines = ctx_lines[:11]
        if ctx_lines:
            self._text_analyzer._ctx_dialog = "\n".join(reversed(ctx_lines))

        now = time.time()
        idle = now - getattr(self, "_last_ts", now)
        self._last_ts = now

        self.state["arousal"] = self.state.get("arousal", 0.5) * pow(0.97, idle / 60)
        self._dirty_metrics.add("arousal")

        self.state["energy"] = self.state.get("energy", 0.0) * pow(0.98, idle / 60)
        self._dirty_metrics.add("energy")

        self.state["fatigue"] = self.state.get("fatigue", 0.0) * pow(settings.FATIGUE_RECOVERY_RATE, idle / 60)
        self._dirty_metrics.add("fatigue")

        val_homeo = pow(settings.VALENCE_HOMEOSTASIS_DECAY, idle / 60)
        self.state["valence"] = self.state.get("valence", 0.0) * val_homeo
        self._dirty_metrics.add("valence")

        decay_neg = pow(settings.EMO_PASSIVE_DECAY, idle / 60)
        for emo in ("stress", "anxiety", "anger"):
            self._dirty_metrics.add(emo)
            self.state[emo] = self.state.get(emo, 0.0) * decay_neg

        try:
            local_tz = ZoneInfo(getattr(settings, "DEFAULT_TZ", "UTC"))
            now_loc  = datetime.now(local_tz)
            tz_hour  = now_loc.hour + now_loc.minute / 60
        except Exception:
            tz_hour  = (time.time() / 3600) % 24
        circadian = settings.CIRCADIAN_AMPLITUDE * math.sin((tz_hour - 3) / 24 * 2 * pi)
        self.state["arousal"] = self._clamp(self.state.get("arousal", 0.5) + circadian)
        self._dirty_metrics.add("arousal")

        idle = now - getattr(self, "_last_mood_change_ts", now)
        if idle > 300:
            self.state["valence"] = self.state.get("valence", 0.0) * settings.VALENCE_HOMEOSTASIS_DECAY ** (idle/60) * 1.2
        if self.mood != getattr(self, "_prev_mood", None):
            self._last_mood_change_ts = now
            self._prev_mood = self.mood

        self._last_uid = uid
        self._last_user_msg = text

        trust = readings.get("trust", 0.5)
        sigma = (1.0 - trust) * 0.05
        for m in ANALYSIS_METRICS:
            if m == "valence":
                continue
            readings[m] = self._clamp(readings[m] + self._rng.gauss(0.0, sigma))

        readings["energy"] = self._clamp(len(text)/200 + excl*0.02 + ques*0.01)

        try:
            cur_v = readings.get("valence", 0.0)
            if "valence" not in self.ema:
                self.ema["valence"] = 0.5
            delta_v = abs(cur_v - self.ema["valence"])
            alpha_v = max(0.10, min(0.25, settings.EMO_EMA_ALPHA * (1 + 1.5 * delta_v)))
            self.ema["valence"] = self.ema["valence"] * (1 - alpha_v) + cur_v * alpha_v
            readings["valence"] = self._clamp(self.ema["valence"])
        except Exception:
            pass

        if readings.get("arousal", 0.0) > settings.FATIGUE_AROUSAL_THRESHOLD or readings["energy"] > settings.FATIGUE_ENERGY_THRESHOLD:
            delta_f = settings.FATIGUE_ACCUMULATE_RATE * (readings["arousal"] + readings["energy"]) / 2
            self.state["fatigue"] = FAT_CLAMP(self.state.get("fatigue", 0.0) + delta_f)
            self._dirty_metrics.add("fatigue")


        def _compute_states(state, readings, change_rates, rng):

            s = [state[m] for m in ALL_METRICS]
            r = []
            for m in ALL_METRICS:
                rv = readings.get(m, 0.0)
                if m == "valence" and 0.0 <= rv <= 1.0:
                    rv = rv * 2.0 - 1.0
                r.append(rv)
            N = len(s)
            new = {}
            for i, m in enumerate(ALL_METRICS):
                base = EMO_DT_BASE.get(m, EMO_DT_DEFAULT)
                rate = change_rates.get(m, 1.0)
                if m == "valence":
                    dt = base * rate * (1.0 - min(1.0, abs(s[i])))
                else:
                    dt = base * rate * (1.0 - s[i])
                dotA = sum(A[i][j] * s[j] for j in range(N))
                dotB = sum(B[i][j] * (r[j] - s[j]) for j in range(N))
                x = s[i] + dt * (dotA + dotB)
                if m == "valence":
                    new[m] = max(-1.0, min(1.0, x))
                else:
                    new[m] = max(0.0, min(1.0, x))
            return new

        loop = asyncio.get_running_loop()
        computed = await loop.run_in_executor(
            EXECUTOR, _compute_states, dict(self.state), readings, self.change_rates, self._rng
        )

        raw_deltas = apply_triggers(text)
        trigger_deltas = {m: d for m, d in raw_deltas.items() if m in ALL_METRICS}
        imp = self._compute_salience(readings, text)
        factor_imp = settings.APPRAISAL_IMPORTANCE_FACTOR * (1 - self.state.get("fatigue", 0.0))

        async with self._lock:
            weight = self._update_weight(uid)

            for m, val in computed.items():
                self.state[m] = val
                self._dirty_metrics.add(m)
            suppress_opposite(None, self.state)

            for metric, delta in trigger_deltas.items():
                if imp > 0.8:
                    base_rate = self.change_rates.get(metric, 1.0)
                    raw = self.state.get(metric, 0.0) + delta * base_rate
                    lo, hi = (-1.0, 1.0) if metric == "valence" else (0.0, 1.0)
                    self.state[metric] = self._clamp(raw, lo, hi)
                    self._dirty_metrics.add(metric)
                    suppress_opposite(metric, self.state)
                else:
                    self._blend_metric(metric, self.state[metric] + delta, weight)

            self._update_mood_label()
            self._compute_secondary()
            self._compute_tertiary()

            self.state["arousal"] = self._clamp(self.state.get("arousal", 0.5) + factor_imp * imp)
            self._dirty_metrics.add("arousal")

            expc = readings.get("anticipation", 0.5)
            surprise_delta = (1 - expc) * settings.APPRAISAL_EXPECTATION_FACTOR * (1 - self.state["fatigue"])
            self.state["surprise"] = self._clamp(self.state.get("surprise", 0.0) + surprise_delta)
            self._dirty_metrics.add("surprise")

            ctrl = readings.get("trust", 0.5) - readings.get("fear", 0.5)
            self.state["dominance"] = self._clamp(self.state.get("dominance", 0.5) + settings.APPRAISAL_CONTROL_FACTOR * ctrl)
            self._dirty_metrics.add("dominance")

            if readings.get("fear",0.0) > 0.8:
                drop = (readings["fear"] - 0.8)**2
                self.state["dominance"] *= (1 - drop)
                self._dirty_metrics.add("dominance")

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

        try:
            emb_ready = await get_embedding(text)
            self._bg_queue.put_nowait((text, readings, emb_ready))
        except asyncio.QueueFull:
            logger.warning("BG-queue full → memory save skipped")
        finally:
            self.state_version += 1


async def _bg_worker(self) -> None:

    await self.enhanced_memory.ready()
    while True:
        text, readings, emb_opt = await self._bg_queue.get()
        try:
            emb = emb_opt or await asyncio.wait_for(get_embedding(text), timeout=15)
            state_slice = {
                "arousal": self.state.get("arousal", 0.0),
                "valence": self.state.get("valence", 0.0),
                "stress":  self.state.get("stress",  0.0),
            }
            await self.enhanced_memory.record(
                text=text,
                embedding=emb,
                emotions={e: readings.get(e, 0.0) for e in PRIMARY_EMOTIONS},
                state_metrics=state_slice,
            )
        except Exception:
            logger.exception("BG-worker: Error processing task")
        finally:
            self._bg_queue.task_done()
EOF