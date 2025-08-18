cat >app/emo_engine/persona/states.py<< 'EOF'
#app/emo_engine/persona/states.py
from __future__ import annotations

import asyncio
import time
import math
import json
import logging

from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt, atan2, pi
from typing import Dict, List
from collections import Counter

from app.config import settings
from app.core.memory import load_context
from app.clients.openai_client import _call_openai_with_retry
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


def _ensure_attachment_defaults(self, rec: dict | None, now: float | None = None) -> dict:

    if not isinstance(rec, dict):
        rec = {}
    if now is None:
        now = time.time()

    # flat fields
    rec.setdefault("value", getattr(settings, "ATTACHMENT_INIT", 0.1))
    rec.setdefault("vel", 0.0)
    rec.setdefault("ts", now)
    rec.setdefault("rupture", 0)
    rec.setdefault("recovery", 0.0)
    rec.setdefault("born_ts", now)
    rec.setdefault("trust_ema", 0.5)
    rec.setdefault("style", "secure")
    rec.setdefault("style_conf", 0.0)
    rec.setdefault("pos_accum", 0.0)

    # nested signals
    s = rec.get("signals")
    if not isinstance(s, dict):
        s = {}
    s.setdefault("samples", 0)
    s.setdefault("q", 0)
    s.setdefault("apol", 0)
    s.setdefault("clingy", 0)
    s.setdefault("boundary", 0)
    rec["signals"] = s
    return rec


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
        + sum(abs(readings.get(e, 0.5) - 0.5) for e in PRIMARY_EMOTIONS if e != "valence")
    )
    base = (numer / (len(PRIMARY_EMOTIONS) + 1)) / max(1.0, len(text) ** 0.5)
    bonus = (
        text.count("!") * 0.02
      + text.count("?") * 0.01
      + sum(text.count(e) for e in ("😀","😂","😢","😡","😱","👍","👎","❤️")) * 0.01
    )
    salience = min(1.0, base + min(0.15, bonus))
    salience = pow(salience, 1.07)
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
    r     = sqrt(x*x + y*y)
    
    if r > 0.75:
        strength = "strong"
    elif r > 0.5:
        strength = "moderate"
    else:
        strength = "weak"

    self.mood = f"{strength}_{base}"


def _attachment_label(x: float) -> str:
    thresholds = [
        (0.05, "Stranger"),
        (0.15, "Familiar"),
        (0.30, "CasualFriend"),
        (0.45, "Friendly"),
        (0.60, "Warm"),
        (0.75, "Trusted"),
        (0.88, "Close"),
        (0.95, "VeryClose"),
        (0.99, "Attached"),
    ]
    for thr, name in thresholds:
        if x < thr:
            return name
    return "Bonded"


def _update_attachment(self, uid: int, readings: Dict[str, float], imp: float, weight: float | None = None) -> None:

    now = time.time()
    rec = self._ensure_attachment_defaults(self.attachments.get(uid), now)
    self.attachments[uid] = rec

    dt = max(0.0, now - rec.get("ts", now))
    rec["ts"] = now

    tau = float(getattr(settings, "ATTACHMENT_TIME_TAU", 120.0))
    time_gate = 1.0 - math.exp(-dt / max(1.0, tau))

    baseline    = getattr(settings, "ATTACHMENT_BASELINE", 0.1)
    pos_rate    = getattr(settings, "ATTACHMENT_POS_RATE", 0.022)
    neg_rate    = getattr(settings, "ATTACHMENT_NEG_RATE", 0.03)
    alpha_pos   = getattr(settings, "ATTACHMENT_POS_EXP",  1.08)
    beta_neg    = getattr(settings, "ATTACHMENT_NEG_EXP",  1.18)
    neutral_leak = getattr(settings, "ATTACHMENT_NEUTRAL_LEAK", 0.0018)
    neg_bias_neutral = getattr(settings, "ATTACHMENT_NEG_BIAS_NEUTRAL", 0.22)
    half_life  = float(getattr(settings, "ATTACHMENT_IDLE_HALFLIFE", 21 * 86400))
    rup_need   = getattr(settings, "ATTACHMENT_RUPTURE_REPAIR", 0.030)
    rup_imp_thr = getattr(settings, "ATTACHMENT_RUPTURE_SALIENCE", 0.85)
    rup_val_thr = getattr(settings, "ATTACHMENT_RUPTURE_VALENCE", 0.60)
    eps        = float(getattr(settings, "ATTACHMENT_VALENCE_EPS", 0.07))
    max_step   = float(getattr(settings, "ATTACHMENT_MAX_STEP", 0.03))
    rup_cooldown_base = int(getattr(settings, "ATTACHMENT_RUPTURE_COOLDOWN", 3600))
    rup_drop_base     = float(getattr(settings, "ATTACHMENT_RUPTURE_DROP", 0.20))
    vel_beta     = float(getattr(settings, "ATTACHMENT_VEL_BETA", 0.3))

    x = float(rec["value"])
    x_prev = x

    if half_life > 0 and dt > 0:
        decay = pow(0.5, dt / half_life)
        x = baseline + (x - baseline) * decay

    v  = float(readings.get("valence", 0.5)) * 2 - 1
    ar = float(readings.get("arousal", 0.5))
    trust = float(rec.get("trust_ema", readings.get("trust", 0.5)))
    intensity = max(0.0, min(1.0, 0.6 * abs(v) + 0.4 * ar))

    w = weight if weight is not None else self._decayed_weight(uid)
    pos_k = pos_rate * (0.5 + 0.8 * imp) * (0.7 + 0.6 * trust) * (0.9 + 0.4 * w)
    neg_k = neg_rate * (0.7 + 1.0 * imp) * (0.9 + 0.6 * (1 - trust)) * (1.05 - 0.3 * w)

    if v > eps:
        dpos = (1.0 - x) * pos_k * (pow(intensity, alpha_pos))
        if rec.get("rupture", 0) > 0:
            dpos *= 0.4
            rec["recovery"] = rec.get("recovery", 0.0) + dpos * time_gate
            if rec.get("rupture_until", 0) > now:
                dpos *= 0.25
            elif rec["recovery"] >= rup_need:
                rec["rupture"] = 0
                rec["recovery"] = 0.0
                rec["rupture_until"] = 0.0
        x += dpos * time_gate
    elif v < -eps:
        neutral_factor = 1.0 + neg_bias_neutral * (1.0 - min(1.0, abs(x - 0.5) * 2.0))
        dneg = max(0.0, x - baseline) * neg_k * pow(intensity, beta_neg) * neutral_factor
        x -= dneg * time_gate
        if imp >= rup_imp_thr and abs(v) >= rup_val_thr:
            rec["rupture"] = min(2, rec.get("rupture", 0) + 1)
            rec["recovery"] = 0.0
            mult = 1.0 + 0.5 * max(0, rec["rupture"] - 1)
            adj_drop = max(0.0, min(1.0, rup_drop_base * mult))
            x = baseline + (x - baseline) * (1.0 - adj_drop * intensity)
            rec["rupture_until"] = now + int(rup_cooldown_base * mult)
    else:
        leak = neutral_leak * (0.3 + 0.7 * imp)
        x = baseline + (x - baseline) * (1.0 - leak * time_gate)

    vel = float(rec.get("vel", 0.0))
    x_smooth = x_prev + ((1.0 - vel_beta) * vel + vel_beta * (x - x_prev))
    dx = x_smooth - x_prev
    dx = max(-max_step, min(max_step, dx))
    x_final = x_prev + dx
    new_vel = (1.0 - vel_beta) * vel + vel_beta * (x_final - x_prev)
    x = x_final
    rec["vel"] = new_vel
    rec["value"] = max(0.0, min(1.0, x))
    new_stage = _attachment_label(rec["value"])
    prev_stage = rec.get("stage")
    if prev_stage and prev_stage != new_stage:
        STAGE_HYST = float(getattr(settings, "ATTACHMENT_STAGE_HYST", 0.01))
        bounds = [0.05, 0.15, 0.30, 0.45, 0.60, 0.75, 0.88, 0.95, 0.99]
        b = min(bounds, key=lambda t: abs(t - rec["value"]))
        if abs(rec["value"] - b) < STAGE_HYST:
            new_stage = prev_stage
    rec["stage"] = new_stage
    self.attachments[uid] = rec

    if getattr(settings, "ATTACHMENT_PERSIST", False):
        try:
            asyncio.create_task(self._persist_attachment(uid))
        except Exception:
            logger.debug("persist_attachment schedule failed", exc_info=True)


async def _detect_social_signals_llm(self, text: str, *, timeout: float | None = None) -> Dict[str, bool]:

    sys_msg = (
        "You are a multilingual classifier. Read and understand the user's message in ANY language, "
        "but produce a STRICT English ASCII JSON output only.\n\n"
        "Detect whether the message contains these pragmatic signals (set all that apply):\n"
        "- apology\n"
        "- promise or commitment\n"
        "- fulfillment report (e.g., 'I did it' / 'it's done')\n"
        "- clingy seeking attention (e.g., 'are you there?', 'reply please')\n"
        "- boundary request (e.g., 'need space', 'not comfortable')\n\n"
        "OUTPUT POLICY:\n"
        "- Return ONLY a single minified JSON object with EXACT lowercase ASCII keys: "
        "apology, promise, fulfill, clingy, boundary.\n"
        "- Values MUST be integers 0 or 1. Default to 0 if uncertain.\n"
        "- NO code fences, NO comments, NO prose, NO trailing commas.\n"
        "- Example valid output: {\"apology\":0,\"promise\":1,\"fulfill\":0,\"clingy\":0,\"boundary\":0}"
    )
    try:
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.BASE_MODEL,
                    messages=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": text},
                    ],
                    max_completion_tokens=40,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                ),
                timeout=5,
            )
        except Exception:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.BASE_MODEL,
                    messages=[
                        {"role": "system", "content": sys_msg},
                        {"role": "user", "content": text},
                    ],
                    max_completion_tokens=40,
                    temperature=0.0,
                ),
                timeout=5,
            )
        raw = (resp.choices[0].message.content or "").strip()
        raw = raw.lstrip("\ufeff").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if "\n" in raw:
                raw = raw.split("\n", 1)[1]
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        def _truth(v):
            if isinstance(v, bool):
                return 1 if v else 0
            if isinstance(v, (int, float)):
                return 1 if float(v) >= 0.5 else 0
            s = str(v or "").strip().lower()
            return 1 if s in ("1","true","yes","y","yeah","sure","si","sí","oui","ja","да","так","是","對","对","sim") else 0
        out = {
            "apology":  _truth(data.get("apology", 0)),
            "promise":  _truth(data.get("promise", data.get("commitment", 0))),
            "fulfill":  _truth(data.get("fulfill", data.get("done", 0))),
            "clingy":   _truth(data.get("clingy", data.get("seeking_attention", 0))),
            "boundary": _truth(data.get("boundary", data.get("need_space", 0))),
        }
        for k in ("apology","promise","fulfill","clingy","boundary"):
            out[k] = 1 if out.get(k, 0) else 0
        return {k: bool(v) for k, v in out.items()}
    except Exception:
        return {}



async def process_interaction(
    self,
    uid: int,
    text: str,
    user_gender: str | None = None,
) -> None:

    async with self._proc_sem:

        try:
            await self._load_attachment(uid)
        except Exception:
            logger.debug("_load_attachment failed", exc_info=True)

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

        signals_llm: Dict[str, bool] = {}
        try:
            if imp >= 0.25 or (self.state_version % 4 == 0):
                signals_llm = await _detect_social_signals_llm(self, text)
        except Exception:
            signals_llm = {}

        async with self._lock:
            rec = self._ensure_attachment_defaults(self.attachments.get(uid), time.time())
            t_ema = float(rec.get("trust_ema", 0.5))
            alpha_t = max(0.05, min(0.25, 0.12 * (1.0 + 0.8 * imp)))
            t_ema = t_ema * (1 - alpha_t) + readings.get("trust", 0.5) * alpha_t
            rec["trust_ema"] = max(0.0, min(1.0, t_ema))
            sig = rec["signals"]
            sig["samples"] = int(sig.get("samples", 0)) + 1
            sig["q"] = int(sig.get("q", 0)) + ques

            if signals_llm.get("apology"):  sig["apol"] = int(sig.get("apol", 0)) + 1
            if signals_llm.get("clingy"):   sig["clingy"] = int(sig.get("clingy", 0)) + 1
            if signals_llm.get("boundary"): sig["boundary"] = int(sig.get("boundary", 0)) + 1
            rec["signals"] = sig
            self.attachments[uid] = rec

            if any((signals_llm.get("apology"), signals_llm.get("promise"), signals_llm.get("fulfill"))):
                try:
                    r = self.attachments[uid]
                    boost = 0.015 + 0.035 * imp
                    r["recovery"] = r.get("recovery", 0.0) + boost
                    if r.get("rupture_until", 0.0) > time.time():
                        r["rupture_until"] = time.time() + max(0.0, (r["rupture_until"] - time.time()) * 0.6)
                    if r.get("recovery", 0.0) >= getattr(settings, "ATTACHMENT_RUPTURE_REPAIR", 0.030):
                        r["rupture"] = 0
                        r["recovery"] = 0.0
                        r["rupture_until"] = 0.0
                    self.attachments[uid] = r
                except Exception:
                    logger.debug("repair-token handling failed", exc_info=True)

            weight = self._update_weight(uid)
            self._update_attachment(uid, readings, imp, weight)

            try:
                MAX_ATTACH = int(getattr(settings, "ATTACHMENT_MAX_USERS", 10000))
            except Exception:
                MAX_ATTACH = 10000
            if len(self.attachments) > MAX_ATTACH:
                by_ts = sorted(self.attachments.items(), key=lambda kv: kv[1].get("ts", 0.0), reverse=True)
                keep = dict(by_ts[:MAX_ATTACH])
                self.attachments = keep

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
                self.ema.setdefault(e, 0.5)
                val_e = readings.get(e, self.ema[e])
                delta = abs(val_e - self.ema[e])
                alpha_dyn = max(0.05, min(0.30, base_alpha * (1 + 2 * delta)))
                self.ema[e] = self.ema[e] * (1 - alpha_dyn) + val_e * alpha_dyn

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
                    self._EMO_LABEL_MAP.get(sorted_em[0][0], sorted_em[0][0]),
                    self._EMO_LABEL_MAP.get(sorted_em[1][0], sorted_em[1][0]),
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
            try:
                uid_cur = getattr(self, "_last_uid", None)
                if uid_cur is not None and uid_cur in self.attachments:
                    state_slice["attachment"] = float(self.attachments[uid_cur].get("value", 0.0))
            except Exception:
                pass
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