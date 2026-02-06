#app/emo_engine/persona/states.py
from __future__ import annotations

import asyncio
import time
import math
import json
import logging
import re

from datetime import datetime
from zoneinfo import ZoneInfo
from math import sqrt, atan2, pi
from typing import Dict, List
from collections import Counter

from app.config import settings
from app.core.memory import load_context
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from .executor import EXECUTOR
from .memory import get_embedding
from .utils.trigger_patterns import apply_triggers
from .constants.temperaments import TEMPERAMENT_PROFILE
from .constants.zodiacs import ZODIAC_MODIFIERS
from .utils.emotion_math import(
    suppress_opposite, EMO_DT_BASE, EMO_DT_DEFAULT, DYNAMIC_METRICS
)
from .utils.emotion_math import EMO_MATRIX_A as A, EMO_MATRIX_B as B
from .constants.emotions import (
    ALL_METRICS, PRIMARY_EMOTIONS, PRIMARY_ORDER, ANALYSIS_METRICS, FAT_CLAMP,
)


logger = logging.getLogger(__name__)

def _ensure_attachment_defaults(self, rec: dict | None, now: float | None = None) -> dict:

    if not isinstance(rec, dict):
        rec = {}
    if now is None:
        now = time.time()

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
    rec.setdefault("rupture_until", 0.0)
    rec.setdefault("stage", "")

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
        prof = TEMPERAMENT_PROFILE.get(t, {})
        for m in ALL_METRICS:
            base[m] *= (prof.get(m, 1.0) ** w)

    zmod = ZODIAC_MODIFIERS.get(self.zodiac, {})
    self.change_rates = {
        m: max(0.25, min(2.5, base[m] * zmod.get(m, 1.0)))
        for m in ALL_METRICS
    }


def _compute_salience(self, readings: Dict[str, float], text: str) -> float:

    core = PRIMARY_ORDER
    numer = (
        abs(readings.get("valence", 0.0))  # уже [-1..1]
        + sum(abs(readings.get(e, 0.5) - 0.5) for e in core)
    )
    base = (numer / (len(core) + 1)) / max(1.0, len(text) ** 0.5)
    bonus = (
        text.count("!") * 0.01
      + text.count("?") * 0.005
      + sum(text.count(e) for e in ("😀","😂","😢","😡","😱","👍","👎","❤️")) * 0.005
    )
    salience = min(1.0, base + min(0.08, bonus))
    salience = pow(salience, 1.07)
    floor = getattr(settings, "MEMORY_MIN_SALIENCE", 0.0)
    return max(floor, salience)


def _decayed_weight(self, uid: int) -> float:

    w, ts = self.user_weights.get(uid, [0.0, time.time()])
    if settings.PERSONA_WEIGHT_HALFLIFE <= 0:
        return w
    now = time.time()
    decayed = max(0.0, w * 0.5 ** ((now - ts) / settings.PERSONA_WEIGHT_HALFLIFE))

    self.user_weights[uid] = [decayed, now]
    return decayed


def _update_weight(self, uid: int, v_opt: float | None = None, imp_opt: float | None = None) -> float:

    cur = self._decayed_weight(uid)
    step = settings.PERSONA_WEIGHT_STEP
    if v_opt is None or imp_opt is None:
        delta = 0.0
    else:
        v = float(v_opt); imp = float(imp_opt)
        if v > 0.07 and imp >= 0.12:
            delta = step * (0.6 + 0.4 * imp)
        elif v < -0.07 and imp >= 0.12:
            delta = - step * (0.5 + 0.5 * imp)
        else:
            delta = 0.0
    new_w = max(0.0, min(1.0, cur + delta))
    self.user_weights[uid] = [new_w, time.time()]
    if len(self.user_weights) > 2000:
        oldest = sorted(self.user_weights.items(), key=lambda kv: kv[1][1])[:200]
        for k, _ in oldest:
            self.user_weights.pop(k, None)
    return new_w


def _effective_person_weight(self, uid: int, base_weight: float) -> float:

    try:
        rec = self.attachments.get(uid) or {}
        att = float(rec.get("value", 0.0))
        trust = float(rec.get("trust_ema", 0.5))
        rupt = int(rec.get("rupture", 0))
        rup_until = float(rec.get("rupture_until", 0.0))
    except Exception:
        att = 0.0; trust = 0.5; rupt = 0; rup_until = 0.0

    mult = 1.0
    if rupt > 0 and time.time() < rup_until:
        mult *= 0.35

    eff = 0.5 * max(0.0, min(1.0, base_weight)) + 0.5 * att
    eff *= (0.7 + 0.3 * trust)
    eff *= mult
    eff *= float(getattr(settings, "ATTACHMENT_WEIGHT_GAIN", 1.0))
    return max(0.0, min(1.0, eff))


def _blend_metric(self, metric: str, target: float, weight: float) -> None:

    if metric == "valence":
        lo, hi = -1.0, 1.0
    else:
        lo, hi = 0.0, 1.0

    cur = self.state.get(metric, 0.0)
    target = max(lo, min(hi, float(target)))

    base_blend = settings.PERSONA_BLEND_FACTOR
    fatigue_penalty = 1.0 - float(self.state.get("fatigue", 0.0))
    influence = base_blend * fatigue_penalty * (1.5 if metric in PRIMARY_EMOTIONS else 1.0)
    influence *= max(0.0, float(weight))
    influence = min(1.0, influence)

    dist = abs(target - cur)
    if dist > 0.8:
        influence = min(1.0, influence * 1.35)

    raw_val = cur * (1.0 - influence) + target * influence

    cr = max(1e-3, float(self.change_rates.get(metric, 1.0)))
    max_alpha  = getattr(settings, "STATE_EMA_MAX_ALPHA", 0.28)
    base_alpha = getattr(settings, "STATE_EMA_ALPHA", 0.12)
    alpha_dyn = min(max_alpha, base_alpha * cr)
    if dist > 0.33:
        alpha_dyn = settings.STATE_EMA_MAX_ALPHA

    new_val = cur * (1.0 - alpha_dyn) + raw_val * alpha_dyn

    if metric == "valence":
        now = time.time()
        peak_enter = 0.85
        peak_exit = 0.80
        hold = now - getattr(self, "_last_valence_peak_ts", now)
        amp = abs(new_val)
        if amp > peak_enter:
            factor = min(1.0, hold / 60.0)
            excess = max(0.0, amp - peak_enter)
            damp = 0.05 * (0.5 + excess)
            new_val -= factor * damp * (1 if new_val > 0 else -1)
        if amp > peak_enter and not getattr(self, "_in_peak", False):
            self._last_valence_peak_ts = now
            self._in_peak = True
        elif amp < peak_exit:
            self._in_peak = False

    self.state[metric] = max(lo, min(hi, new_val))
    self._dirty_metrics.add(metric)
    suppress_opposite(metric, self.state)


def _update_mood_label(self) -> None:
    
    x = float(self.state.get("valence", 0.0))
    y = float(self.state.get("arousal", 0.5)) * 2 - 1
    r = sqrt(x * x + y * y)
    neutral_r = float(getattr(settings, "MOOD_NEUTRAL_RADIUS", 0.18))
    if r < neutral_r:
        self.mood = "steady"
        return

    theta = atan2(y, x) % (2 * pi)

    prims = list(PRIMARY_ORDER)
    idx   = round(theta / (2 * pi) * len(prims)) % len(prims)
    base  = prims[idx]
    
    if r > 0.75:
        strength = "strong"
    elif r > 0.5:
        strength = "moderate"
    else:
        strength = "weak"

    self.mood = f"{strength}_{base}"


def _attachment_label(x: float) -> str:
    thresholds = [
        (0.32, "Stranger"),
        (0.44, "Familiar"),
        (0.55, "Casual"),
        (0.65, "Friendly"),
        (0.74, "Warm"),
        (0.82, "Trust"),
        (0.89, "Close"),
        (0.95, "VeryClose"),
        (0.99, "Attached"),
    ]
    for thr, name in thresholds:
        if x < thr:
            return name
    return "Bonded"


def _apply_attachment_influence(self, uid: int, eff_weight: float) -> None:
    try:
        rec = self.attachments.get(uid) or {}
        att = float(rec.get("value", 0.0))
        trust = float(rec.get("trust_ema", 0.5))
        rupt = int(rec.get("rupture", 0))
        rup_until = float(rec.get("rupture_until", 0.0))
    except Exception:
        att = 0.0; trust = 0.5; rupt = 0; rup_until = 0.0

    w = max(0.0, min(1.0, eff_weight)) * float(getattr(settings, "ATTACHMENT_BEHAVIOR_GAIN", 1.0))
    if w <= 1e-6:
        return

    cooling = (rupt > 0 and time.time() < rup_until)
    phase = 0.4 if cooling else 1.0

    friendly   = 0.25 + 0.60 * att
    empathy    = 0.25 + 0.60 * att
    civ        = 0.30 + 0.45 * att
    patience   = 0.35 + 0.45 * att
    engagement = 0.30 + 0.55 * att
    sarcasm    = max(0.05, 0.20 - 0.18 * att)
    profanity  = max(0.01, 0.05 - 0.04 * att)
    aggress    = max(0.02, 0.10 - 0.08 * att)
    flirt      = self._clamp(0.15 + 0.30 * att, 0.0, 1.0)

    if cooling:
        friendly *= 0.75
        empathy  *= 0.85
        flirt    *= 0.50

    self._blend_metric("friendliness",    friendly,   w * phase)
    self._blend_metric("empathy",         empathy,    w * phase)
    self._blend_metric("civility",        civ,        w * 0.8 * phase)
    self._blend_metric("patience",        patience,   w * phase)
    self._blend_metric("engagement",      engagement, w * phase)
    self._blend_metric("sarcasm",         sarcasm,    w)
    self._blend_metric("profanity",       profanity,  w)
    self._blend_metric("aggressiveness",  aggress,    w)
    try:
        if "flirtation" in self.state:
            self._blend_metric("flirtation", flirt,   w * 0.7 * phase)
    except Exception:
        pass


def _update_attachment(self, uid: int, readings: Dict[str, float], imp: float, weight: float | None = None) -> None:

    now = time.time()
    rec = self._ensure_attachment_defaults(self.attachments.get(uid), now)

    self.attachments[uid] = rec

    dt = max(0.0, now - rec.get("ts", now))
    rec["ts"] = now

    tau = float(getattr(settings, "ATTACHMENT_TIME_TAU", 240.0))
    time_gate = 1.0 - math.exp(-dt / max(1.0, tau))

    baseline = getattr(settings, "ATTACHMENT_BASELINE", 0.1)
    pos_rate = getattr(settings, "ATTACHMENT_POS_RATE", 0.01)
    neg_rate = getattr(settings, "ATTACHMENT_NEG_RATE", 0.019)
    alpha_pos = getattr(settings, "ATTACHMENT_POS_EXP",  1.08)
    beta_neg = getattr(settings, "ATTACHMENT_NEG_EXP",  1.18)
    neutral_leak = getattr(settings, "ATTACHMENT_NEUTRAL_LEAK", 0.0018)
    neg_bias_neutral = getattr(settings, "ATTACHMENT_NEG_BIAS_NEUTRAL", 0.22)
    half_life = float(getattr(settings, "ATTACHMENT_IDLE_HALFLIFE", 21 * 86400))
    rup_need = getattr(settings, "ATTACHMENT_RUPTURE_REPAIR", 0.030)
    rup_imp_thr = getattr(settings, "ATTACHMENT_RUPTURE_SALIENCE", 0.85)
    rup_val_thr = getattr(settings, "ATTACHMENT_RUPTURE_VALENCE", 0.60)
    eps = float(getattr(settings, "ATTACHMENT_VALENCE_EPS", 0.07))
    max_step = float(getattr(settings, "ATTACHMENT_MAX_STEP", 0.015))
    rup_cooldown_base = int(getattr(settings, "ATTACHMENT_RUPTURE_COOLDOWN", 3600))
    rup_drop_base = float(getattr(settings, "ATTACHMENT_RUPTURE_DROP", 0.20))
    vel_beta = float(getattr(settings, "ATTACHMENT_VEL_BETA", 0.3))
    accum_tau = float(getattr(settings, "ATTACHMENT_POS_ACCUM_TAU", 3600.0))
    cap_per_hr = float(getattr(settings, "ATTACHMENT_POS_CAP_PER_HOUR", 0.06))
    rec["pos_accum"] = float(rec.get("pos_accum", 0.0)) * math.exp(-dt / max(1.0, accum_tau))

    x = float(rec["value"])
    x_prev = x

    sig_now = readings.get("_signals_now", {}) or {}

    if half_life > 0 and dt > 0:
        decay = pow(0.5, dt / half_life)
        x = baseline + (x - baseline) * decay

    v  = float(readings.get("valence", 0.0))
    ar = float(readings.get("arousal", 0.5))
    trust = float(rec.get("trust_ema", readings.get("trust", 0.5)))
    intensity = max(0.0, min(1.0, 0.6 * abs(v) + 0.4 * ar))

    if sig_now.get("boundary"):
        rec["rupture"] = min(2, int(rec.get("rupture", 0)) + 1)
        rec["recovery"] = 0.0
        mult = 1.0 + 0.5 * max(0, rec["rupture"] - 1)
        adj_drop = max(0.0, min(1.0, rup_drop_base * (1.0 + 0.25 * (1.0 - trust))))
        x = baseline + (x - baseline) * (1.0 - adj_drop * (0.5 + 0.5 * intensity))
        rec["rupture_until"] = now + int(rup_cooldown_base * mult)

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
        dpos_gated = dpos * time_gate
        if cap_per_hr > 0:
            allowed = max(0.0, cap_per_hr - float(rec.get("pos_accum", 0.0)))
            if dpos_gated > allowed:
                dpos_gated = allowed
            rec["pos_accum"] = float(rec.get("pos_accum", 0.0)) + dpos_gated
        x += dpos_gated
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
        dist = abs(x - baseline)
        leak = neutral_leak * (0.3 + 0.7 * imp) * (0.4 + 0.6 * dist)
        x = baseline + (x - baseline) * (1.0 - leak * time_gate)

    if imp < 0.05 and x < x_prev and (-eps < v < eps):
        x = max(x, x_prev - 0.25 * max_step)

    vel = float(rec.get("vel", 0.0))
    x_smooth = x_prev + ((1.0 - vel_beta) * vel + vel_beta * (x - x_prev))
    dx = x_smooth - x_prev
    eff_max_step = max_step * (0.70 if x_prev >= 0.75 else 1.0)
    dx = max(-eff_max_step, min(eff_max_step, dx))
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

    try:
        if getattr(self, "_last_uid", None) == uid:
            self.state["attachment"] = rec["value"]
            self._dirty_metrics.add("attachment")
    except Exception:
        pass

    if getattr(settings, "ATTACHMENT_PERSIST", False) and hasattr(self, "_persist_attachment"):
        try:
            self.spawn_coro(self._persist_attachment, uid, name="persist-attachment")
        except Exception:
            logger.debug("persist_attachment schedule failed", exc_info=True)


def _compute_states(state, readings, change_rates):

    if A is None or B is None:
        return dict(state)

    try:
        max_dim = min(len(DYNAMIC_METRICS), len(A), len(B))
    except Exception:
        max_dim = len(DYNAMIC_METRICS)

    if len(DYNAMIC_METRICS) != len(A) or len(A) != len(B):
        logger.warning(
            "DYNAMIC_METRICS(%d) ≠ A(%d) or B(%d) – use the first %d elements",
            len(DYNAMIC_METRICS), len(A), len(B), max_dim,
        )

    metrics = DYNAMIC_METRICS[:max_dim]

    s = [state.get(m, 0.0) for m in metrics]
    r = [readings.get(m, state.get(m, 0.0)) for m in metrics]

    N = len(s)
    new: dict[str, float] = {}
    integrator = getattr(settings, "EMO_INTEGRATOR", "euler")

    for i, m in enumerate(metrics):
        if m == "attachment":
            new[m] = state.get("attachment", 0.0)
            continue

        base = EMO_DT_BASE.get(m, EMO_DT_DEFAULT)
        rate = change_rates.get(m, 1.0)

        if base <= 0.0:
            new[m] = state.get(m, 0.0)
            continue

        if m == "valence":
            cur_val = s[i]
            dt = base * rate * (1.0 - min(1.0, abs(cur_val)))
        else:
            cur_val = s[i]
            dt = base * rate * (1.0 - cur_val)

        DT_MIN = 1e-3
        dt = max(DT_MIN, min(dt, 1.0))

        rowA = A[i]
        rowB = B[i]
        inner_len = min(
            N,
            len(rowA),
            len(rowB),
        )
        dotA_full = sum(rowA[j] * s[j] for j in range(inner_len))
        dotB      = sum(rowB[j] * r[j] for j in range(inner_len))

        if integrator == "semi_implicit":
            a_diag = rowA[i] if i < len(rowA) else 0.0
            dotA_wo_diag = dotA_full - a_diag * s[i]
            numer = s[i] + dt * (dotA_wo_diag + dotB)
            denom = max(1e-6, 1.0 - dt * a_diag)
            x = numer / denom
        else:
            x = s[i] + dt * (dotA_full + dotB)

        if m == "valence":
            new[m] = max(-1.0, min(1.0, x))
        else:
            new[m] = max(0.0, min(1.0, x))

    return new

async def _detect_social_signals_llm(self, text: str, *, timeout: float | None = None) -> Dict[str, bool]:

    system_prompt = (
        "You are a multilingual, deterministic text classifier. Read a single user message "
        "and decide whether it explicitly contains each of the following pragmatic signals. "
        "You MUST output EXACTLY ONE minified JSON object that STRICTLY conforms to the provided JSON schema.\n"
        "\n"
        "Signals (set 1 if present explicitly, else 0):\n"
        "- apology: explicit remorse words (e.g., 'sorry', 'apologize', 'извини', 'простите', 'сорри', 'my bad').\n"
        "- promise: explicit future commitment (e.g., 'I will/I'll', 'I promise', 'обещаю', 'сделаю'). "
        "  Exclude questions/hedges like 'maybe', 'I'll try', 'should I'.\n"
        "- fulfill: explicit completion report (e.g., 'done', 'finished', 'готово', 'сделал', 'completed', 'delivered').\n"
        "- clingy: attention-seeking to keep engaging (e.g., 'please reply', 'are you there', 'ответь', repeated '???'/'pls', 'не уходи').\n"
        "- boundary: explicit limits/refusals or stop requests (e.g., 'I won't', 'don't contact me', 'не пиши', 'не буду', 'stop').\n"
        "\n"
        "Rules:\n"
        "- Output JSON only (one line), no prose/markdown, ASCII keys only, integer values 0/1.\n"
        "- Base the decision on THIS message only; ignore prior context; do not infer unstated intent.\n"
        "- Hypotheticals, quotes, or uncertainty → 0.\n"
        "- Multiple signals may be 1 simultaneously."
    )
    user_prompt = (
        f"INPUT:\n{text}\n\nReturn ONLY a single minified JSON object."
    )

    schema = {
        "type": "object",  
        "properties": {
            "apology":  {"type": "integer", "minimum": 0, "maximum": 1},
            "promise":  {"type": "integer", "minimum": 0, "maximum": 1},
            "fulfill":  {"type": "integer", "minimum": 0, "maximum": 1},
            "clingy":   {"type": "integer", "minimum": 0, "maximum": 1},
            "boundary": {"type": "integer", "minimum": 0, "maximum": 1},
        },
        "required": ["apology", "promise", "fulfill", "clingy", "boundary"],
        "additionalProperties": False,
    }
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                instructions=system_prompt,
                input=user_prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "social_signals",
                        "schema": schema,
                        "strict": True
                    }
                },
                temperature=0,
                max_output_tokens=200,
            ),
            timeout=timeout or 30.0,
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "openai.request endpoint=responses.create model=%s duration_ms=%.2f timeout_s=%.2f chat_id=%s",
            getattr(settings, "BASE_MODEL", None),
            dt_ms,
            (timeout or 30.0),
            getattr(self, "chat_id", None),
        )
        raw = (_get_output_text(resp) or "").strip()
        if raw.startswith("```"):
            try:
                nl = raw.find("\n")
                if nl != -1:
                    raw = raw[nl + 1 :]
                raw = raw.rstrip("`").strip()
            except Exception:
                pass
        raw = raw.lstrip("\ufeff")
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{") : raw.rfind("}") + 1]
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
            "promise":  _truth(data.get("promise", 0)),
            "fulfill":  _truth(data.get("fulfill", 0)),
            "clingy":   _truth(data.get("clingy", 0)),
            "boundary": _truth(data.get("boundary", 0)),
        }
        for k in ("apology","promise","fulfill","clingy","boundary"):
            out[k] = 1 if out.get(k, 0) else 0
        return {k: bool(v) for k, v in out.items()}
    except Exception:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning(
            "openai.request endpoint=responses.create model=%s failed_after_ms=%.2f timeout_s=%.2f chat_id=%s",
            getattr(settings, "BASE_MODEL", None),
            dt_ms,
            (timeout or 30.0),
            getattr(self, "chat_id", None),
            exc_info=True,
        )
        logger.debug("_detect_social_signals_llm failed — returning empty signals", exc_info=True)
        return {}


async def process_interaction(
    self,
    uid: int,
    text: str,
    user_gender: str | None = None,
) -> None:

    async with self._proc_sem:
        return await _process_interaction_unlocked(self, uid, text, user_gender=user_gender)


async def _process_interaction_unlocked(
    self,
    uid: int,
    text: str,
    user_gender: str | None = None,
) -> None:

    try:
        if getattr(self, "_selfnet_disabled", False):
            if (self.state_version % 17) == 0:
                setattr(self, "_selfnet_disabled", False)
    except Exception:
        pass

    try:
        await self._load_attachment(uid)
    except Exception:
        logger.debug("_load_attachment failed", exc_info=True)

    local_gender = user_gender if user_gender is not None else self.user_gender
    self.user_gender = local_gender

    defaults = {
        "valence":          0.60,
        "arousal":          0.56,
        "dominance":        0.50,
        "energy":           0.55,
        "fatigue":          0.02,
        "stress":           0.03,
        "anxiety":          0.04,
        "joy":              0.55,
        "optimism":         0.58,
        "trust":            0.42,
        "civility":         0.62,
        "confidence":       0.58,
        "friendliness":     0.62,
        "humor":            0.58,
        "wit":              0.58,
        "patience":         0.48,
        "self_reflection":  0.42,
        "precision":        0.52,
        "charisma":         0.60,
        "persuasion":       0.52,
        "authority":        0.46,
        "empathy":          0.50,
        "engagement":       0.60,
        "curiosity":        0.62,
        "sexual_arousal":   0.02,
        "aggressiveness":   0.02,
        "sarcasm":          0.12,
        "profanity":        0.02,
        "self_deprecation": 0.08,
        "flirtation":       0.06,
    }
    if self.state_version == 0 and not getattr(self, "_startup_tuned", False):
        for k in ALL_METRICS:
            if k == "valence":
                v = defaults.get(k, 0.0)
                v = (v * 2.0 - 1.0) if (0.0 <= v <= 1.0) else v
                self.state[k] = self._clamp(v, -1.0, 1.0)
            else:
                self.state[k] = self._clamp(defaults.get(k, self.state.get(k, 0.0)))
        try:
            for e in PRIMARY_EMOTIONS:
                self.ema[e] = self.state.get(e, self.ema.get(e, 0.5))
            for e in ("arousal","energy","fatigue"):
                self.ema[e] = self.state.get(e, self.ema.get(e, 0.5))
            self.ema["valence"] = self.state.get("valence", 0.0)
        except Exception:
            pass
        self._dirty_metrics.update(defaults.keys())
        self._startup_tuned = True

    now = time.time()
    punc = Counter(text)
    excl = punc["!"]
    ques = punc["?"]

    try:
        raw_hist = await asyncio.wait_for(load_context(self.chat_id, uid), timeout=1.5)
    except asyncio.TimeoutError:
        raw_hist = []
    except Exception:
        logger.debug("load_context failed", exc_info=True)
        raw_hist = []

    ROLE = {"user": "USER", "assistant": "ASSISTANT"}
    MAX_CTX = 11
    ctx_lines: list[str] = [f"USER[{time.strftime('%H:%M', time.localtime())}]: {self._safe_snippet(text)}"]
    for m in reversed(raw_hist):
        if len(ctx_lines) >= MAX_CTX:
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

    ctx_dialog = "\n".join(reversed(ctx_lines))

    readings = await self._text_analyzer.analyze_text(text, ctx_dialog=ctx_dialog)

    idle = time.monotonic() - getattr(self, "_last_mono", time.monotonic())
    self._last_mono = time.monotonic()

    self.state["arousal"] = 0.5 + (self.state.get("arousal", 0.5) - 0.5) * pow(getattr(settings, "AROUSAL_HOMEOSTASIS_DECAY", 0.97), idle/60)
    self._dirty_metrics.add("arousal")

    self.state["energy"] = 0.5 + (self.state.get("energy", 0.5) - 0.5) * pow(getattr(settings, "ENERGY_HOMEOSTASIS_DECAY", 0.98), idle/60)
    self._dirty_metrics.add("energy")

    self.state["fatigue"] = self.state.get("fatigue", 0.0) * pow(settings.FATIGUE_RECOVERY_RATE, idle / 60)
    self._dirty_metrics.add("fatigue")

    val_homeo = pow(settings.VALENCE_HOMEOSTASIS_DECAY, idle / 60)
    try:
        v_base = float(getattr(settings, "VALENCE_BASELINE", 0.15))
        if 0.0 <= v_base <= 1.0:
            v_base = v_base * 2.0 - 1.0
    except Exception:
        v_base = 0.15
    cur_v = float(self.state.get("valence", 0.0))
    self.state["valence"] = self._clamp(cur_v * val_homeo + (1.0 - val_homeo) * v_base, -1.0, 1.0)
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
    self.ema.setdefault("arousal", self.state.get("arousal", 0.5))
    self.ema["arousal"] = self._clamp(self.ema["arousal"] + circadian * 0.5)
    self._dirty_metrics.add("arousal")

    idle = now - getattr(self, "_last_mood_change_ts", now)
    if idle > 300:
        v = self.state.get("valence", 0.0) * settings.VALENCE_HOMEOSTASIS_DECAY ** (idle/60)
        self.state["valence"] = self._clamp(v, -1.0, 1.0)
    if self.mood != getattr(self, "_prev_mood", None):
        self._last_mood_change_ts = now
        self._prev_mood = self.mood

    self._last_uid = uid
    self._last_user_msg = text

    try:
        emb_task = getattr(self, "_emb_inflight", None)
        restart = False
        if emb_task is None or emb_task.done():
            restart = True
        else:
            started_ts = getattr(self, "_emb_inflight_started", None)
            if started_ts is None or (time.time() - started_ts) > 20.0:
                try:
                    emb_task.cancel()
                except Exception:
                    pass
                restart = True
        if restart:
            async def _do_embed(msg: str):
                try:
                    return await asyncio.wait_for(get_embedding(msg), timeout=15.0)
                except Exception:
                    return None
            self._emb_inflight_started = time.time()
            self._emb_inflight = asyncio.create_task(
                _do_embed(text), name="persona-last-msg-emb"
            )
            self._emb_inflight_text = text
            def _on_emb_done(task: asyncio.Task, msg: str = text) -> None:
                try:
                    if task.cancelled():
                        return
                    if task.exception() is not None:
                        return
                    result = task.result()
                    if result:
                        self._last_msg_emb = result
                        self._last_msg_emb_text = msg
                except Exception:
                    pass
            self._emb_inflight.add_done_callback(_on_emb_done)

        emb_task = self._emb_inflight
        self._last_msg_emb = (
            emb_task.result()
            if emb_task.done() and not emb_task.cancelled() and emb_task.exception() is None
            else None
        )
    except Exception:
        self._last_msg_emb = None

    imp = self._compute_salience(readings, text)

    try:
        tau = float(getattr(settings, "FLOWSTATE_TAU", 90.0))
        decay = math.exp(-max(0.0, idle) / max(1.0, tau))
        self.flowstate = self._clamp(self.flowstate * decay + (1.0 - decay) * imp)
    except Exception:
        pass

    weight_eff = 0.0
    signals_llm: Dict[str, bool] = {}
    do_signals = False
    now_ts = time.time()
    cur_snippet = self._safe_snippet(text)
    if (imp >= 0.25) or (self.state_version % 6 == 0):
        try:
            async with self._user_lock(uid):
                rec_tmp = self._ensure_attachment_defaults(self.attachments.get(uid), now_ts)
                next_allowed = float(rec_tmp.get("_signals_next_allowed", 0.0))
                if now_ts >= next_allowed:
                    rec_tmp["_signals_next_allowed"] = now_ts + 20.0
                    self.attachments[uid] = rec_tmp
                    do_signals = True
        except Exception:
            do_signals = False
    if do_signals and len(cur_snippet) < 3:
        do_signals = False
    if do_signals:
        try:
            detector = getattr(self, "_detect_social_signals_llm", None)
            if detector is not None:
                signals_llm = await detector(text)
            else:
                signals_llm = await _detect_social_signals_llm(self, text)
        except Exception:
            signals_llm = {}

    trust = readings.get("trust", 0.5)

    try:
        max_sigma = float(getattr(settings, "ANALYSIS_NOISE_STD_MAX", 0.05))
    except Exception:
        max_sigma = 0.05
    if max_sigma < 0.0:
        max_sigma = 0.0
    sigma = (1.0 - trust) * max_sigma

    if sigma > 0.0 and getattr(self, "_rng", None) is not None:
        for m in ANALYSIS_METRICS:
            if m == "valence":
                continue
            base = readings.get(m, self.state.get(m, 0.0))
            readings[m] = self._clamp(base + self._rng.gauss(0.0, sigma))
    else:
        for m in ANALYSIS_METRICS:
            if m == "valence":
                continue
            readings.setdefault(m, self.state.get(m, 0.0))

    readings["energy"] = self._clamp(len(text)/200 + excl*0.02 + ques*0.01)
    if signals_llm:
        readings["_signals_now"] = dict(signals_llm)

    try:
        cur_v = float(readings.get("valence", 0.0))
        if "valence" not in self.ema:
            self.ema["valence"] = 0.0
        delta_v = abs(cur_v - self.ema["valence"])
        alpha_v = max(0.10, min(0.25, settings.EMO_EMA_ALPHA * (1 + 1.5 * delta_v)))
        self.ema["valence"] = self.ema["valence"] * (1 - alpha_v) + cur_v * alpha_v
        readings["valence"] = self._clamp(self.ema["valence"], -1.0, 1.0)
    except Exception:
        pass

    if readings.get("arousal", 0.0) > settings.FATIGUE_AROUSAL_THRESHOLD or readings["energy"] > settings.FATIGUE_ENERGY_THRESHOLD:
        delta_f = settings.FATIGUE_ACCUMULATE_RATE * (readings["arousal"] + readings["energy"]) / 2
        self.state["fatigue"] = FAT_CLAMP(self.state.get("fatigue", 0.0) + delta_f)
        self._dirty_metrics.add("fatigue")

    loop = asyncio.get_running_loop()
    snapshot_version = self.state_version
    computed = await loop.run_in_executor(
        EXECUTOR, _compute_states, dict(self.state), readings, self.change_rates
    )

    raw_deltas = apply_triggers(text)
    trigger_deltas = {m: d for m, d in raw_deltas.items() if m in ALL_METRICS}
    factor_imp = (
        settings.APPRAISAL_IMPORTANCE_FACTOR
        * (1 - self.state.get("fatigue", 0.0))
        * (1.0 + 0.35 * float(getattr(self, "flowstate", 0.0)))
    )

    schedule_ltm = False
    async with self._user_lock(uid):
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
        try:
            if hasattr(self, "ltm") and self.ltm is not None:
                next_ltm = float(rec.get("_ltm_next", 0.0))
                now_ltm = time.time()
                if now_ltm >= next_ltm:
                    rec["_ltm_next"] = now_ltm + float(getattr(settings, "LTM_COOLDOWN_SECS", 90))
                    self.attachments[uid] = rec
                    schedule_ltm = True
        except Exception:
            logger.debug("ltm next scheduling flag failed", exc_info=True)

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

        weight = self._update_weight(uid, readings.get("valence", 0.0), imp)
        self._update_attachment(uid, readings, imp, weight)
        weight_eff = self._effective_person_weight(uid, weight)

    if schedule_ltm:
        try:
            self.spawn_coro(self.ltm.extract_and_upsert, uid, text, name="ltm-extract-upsert")
        except Exception:
            logger.debug("ltm.extract_and_upsert scheduling failed", exc_info=True)

    try:
        MAX_ATTACH = int(getattr(settings, "ATTACHMENT_MAX_USERS", 10000))
    except Exception:
        MAX_ATTACH = 10000
    if len(self.attachments) > MAX_ATTACH:
        async with self._lock:
            by_ts = sorted(self.attachments.items(), key=lambda kv: kv[1].get("ts", 0.0), reverse=True)
            self.attachments = dict(by_ts[:MAX_ATTACH])

    delta_ticks = self.state_version - snapshot_version
    if delta_ticks <= 1:
        atten = 1.0
    else:
        atten = 0.9 if delta_ticks == 2 else 0.8
    for m, val in computed.items():
        if atten != 1.0:
            cur = self.state.get(m, val)
            val = cur + atten * (val - cur)
        self.state[m] = val
        self._dirty_metrics.add(m)
    for m in list(self._dirty_metrics):
        suppress_opposite(m, self.state)

    for metric, delta in trigger_deltas.items():
        if imp > 0.8:
            base_rate = self.change_rates.get(metric, 1.0)
            raw = self.state.get(metric, 0.0) + delta * base_rate
            lo, hi = (-1.0, 1.0) if metric == "valence" else (0.0, 1.0)
            self.state[metric] = self._clamp(raw, lo, hi)
            self._dirty_metrics.add(metric)
            suppress_opposite(metric, self.state)
        else:
            cur = self.state.get(metric, 0.0)
            self._blend_metric(metric, cur + delta, weight_eff)

    if weight_eff:
        self._apply_attachment_influence(uid, weight_eff)

    self._update_mood_label()
    if hasattr(self, "_compute_secondary"):
        self._compute_secondary()
    if hasattr(self, "_compute_tertiary"):
        self._compute_tertiary()

    self.state["arousal"] = self._clamp(
        self.state.get("arousal", 0.5) + factor_imp * imp * (0.4 + 0.2 * (weight_eff or 0.0))
    )
    self._dirty_metrics.add("arousal")

    expc = readings.get("anticipation", 0.5)
    surprise_delta = (1 - expc) * settings.APPRAISAL_EXPECTATION_FACTOR * (1 - self.state["fatigue"])
    self.state["surprise"] = self._clamp(self.state.get("surprise", 0.0) + surprise_delta)
    self._dirty_metrics.add("surprise")

    ctrl = readings.get("trust", 0.5) - readings.get("fear", 0.5)
    self.state["dominance"] = self._clamp(self.state.get("dominance", 0.5) + settings.APPRAISAL_CONTROL_FACTOR * ctrl)
    self._dirty_metrics.add("dominance")

    if readings.get("fear", 0.0) > 0.8:
        drop = (readings["fear"] - 0.8) ** 2
        self.state["dominance"] = self._clamp(
            self.state.get("dominance", 0.5) * (1 - drop)
        )
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

    em_list = [(e, self.ema[e]) for e in PRIMARY_ORDER]
    if em_list:
        mean_  = sum(v for _, v in em_list) / len(em_list)
        std_   = (sum((v - mean_)**2 for _, v in em_list) / len(em_list))**0.5
    else:
        mean_, std_ = 0.0, 0.0

    sigma_k = 0.5 + std_
    hysteresis = settings.EMO_HYSTERESIS_DELTA * (1 + std_)

    thr_on  = mean_ + sigma_k * std_ + hysteresis
    thr_off = mean_ + sigma_k * std_ - hysteresis

    try:
        dom_thr = float(getattr(self, "dominant_threshold", 0.0))
    except Exception:
        dom_thr = 0.0
    if dom_thr > 0.0:
        thr_on = max(thr_on, dom_thr)

    if em_list:
        top_em, top_val = max(em_list, key=lambda kv: kv[1])
    else:
        top_em, top_val = None, 0.0

    if top_em is not None and (not self.dominant_locked and top_val >= thr_on):
        self.dominant_locked = True
        self.current_dominant = top_em
    elif self.dominant_locked and top_val < thr_off:
        self.dominant_locked = False
        self.current_dominant = None

    if self.dominant_locked and self.current_dominant and em_list:
        sorted_em = sorted(em_list, key=lambda kv: kv[1], reverse=True)
        top_labels = []
        for name, _v in sorted_em[:2]:
            top_labels.append(self._EMO_LABEL_MAP.get(name, name))
        self.persona_dominant_emotions = top_labels
    else:
        if self.dominant_locked and not self.current_dominant:
            self.dominant_locked = False
        self.persona_dominant_emotions = []

    selfnet_metrics = {}
    try:
        sn = getattr(self, "self_net", None)
        if sn is not None and not getattr(self, "_selfnet_disabled", False):
            min_s = float(getattr(settings, "SELFNET_MIN_SALIENCE", 0.10))
            if imp >= min_s:
                state_snapshot = dict(self.state)
                selfnet_metrics = await sn.observe(
                    uid=uid,
                    text=text,
                    readings=readings,
                    state=state_snapshot,
                    salience=imp,
                )
    except Exception:
        logger.debug("SelfNeuronNetwork.observe failed", exc_info=True)
        selfnet_metrics = {}
        try:
            self._selfnet_disabled = True
        except Exception:
            pass

    if selfnet_metrics:
        try:
            gain = float(getattr(settings, "SELFNET_GAIN", 0.4))
        except Exception:
            gain = 0.4
        gain = max(0.0, min(0.7, gain))
        soft_cap = {
            "valence": 0.15,
            "energy":  0.20,
            "fatigue": 0.20,
        }
        mode_meta: dict = {}
        for metric, target in selfnet_metrics.items():
            mname = str(metric)
            if mname.startswith("_mode_") or mname == "_mode_id":
                mode_meta[mname] = target
                continue
            try:
                if mname in soft_cap:
                    cur_val = float(self.state.get(mname, 0.0))
                    delta = target - cur_val
                    cap = soft_cap[mname]
                    if abs(delta) > cap:
                        target = cur_val + cap * (1 if delta > 0 else -1)
                self._blend_metric(mname, target, gain)
            except Exception:
                logger.debug("SelfNeuronNetwork blend failed for %s", mname, exc_info=True)

        try:
            mode_id_raw = mode_meta.get("_mode_id")
            if isinstance(mode_id_raw, str) and mode_id_raw:
                self.current_mode_id = mode_id_raw
            elif isinstance(mode_id_raw, (int, float)):
                self.current_mode_id = f"mode_{int(mode_id_raw)}"

            stats: dict = {}
            for key_src, key_dst in (
                ("_mode_coherence", "coherence"),
                ("_mode_novelty", "novelty"),
                ("_mode_complexity", "complexity"),
                ("_mode_intensity", "intensity"),
            ):
                if key_src in mode_meta:
                    try:
                        stats[key_dst] = float(mode_meta[key_src])
                    except Exception:
                        pass
            if stats:
                self.current_mode_stats = stats
        except Exception:
            logger.debug("SelfNeuronNetwork mode snapshot failed", exc_info=True)

    try:
        text_norm = self._normalize_mem_text(text)
        q = getattr(self, "_bg_queue", None)

        if q is None:
            try:
                self._recent_sketch.append({
                    "t": time.time(),
                    "uid": uid,
                    "imp": float(imp),
                    "text": text_norm,
                })
            except Exception:
                pass
        elif q.full() and imp < 0.30:
            logger.warning("BG-queue full → dropped low-salience item (imp=%.2f)", imp)
            try:
                self._recent_sketch.append({
                    "t": time.time(),
                    "uid": uid,
                    "imp": float(imp),
                    "text": text_norm,
                })
            except Exception:
                pass
        else:
            pass_emb = bool(getattr(settings, "BG_QUEUE_PASS_EMB", True))
            emb_val = getattr(self, "_last_msg_emb", None) if pass_emb else None
            q.put_nowait((text_norm, readings, emb_val, imp, uid))
    except asyncio.QueueFull:
        logger.warning("BG-queue full → memory save skipped")
        try:
            fallback_txt = text_norm if 'text_norm' in locals() else self._normalize_mem_text(text)
            self._recent_sketch.append({
                "t": time.time(),
                "uid": uid,
                "imp": float(imp),
                "text": fallback_txt,
            })
        except Exception:
            pass
    finally:
        self.state_version += 1


async def _bg_worker(self) -> None:
    await self.enhanced_memory.ready()
    n = int(getattr(settings, "BG_WORKER_CONCURRENCY", 8))
    if n < 1:
        n = 1

    async def _process_bg_item(text, readings, emb_opt, imp, uid_cur):
        try:
            base_thr = float(getattr(settings, "MEMORY_MIN_SALIENCE_TO_STORE", 0.12))
            occ = self._bg_queue.qsize() / max(1, self._bg_queue.maxsize)
            min_store = min(0.90, base_thr + 0.20 * occ)
        except Exception:
            min_store = 0.12
        try:
            att = 0.0
            if uid_cur is not None:
                rec = self.attachments.get(uid_cur)
                if isinstance(rec, dict):
                    att = float(rec.get("value", 0.0))
            min_store = max(0.05, min(0.70, min_store - 0.06 * att))
        except Exception:
            pass
        try:
            fs = float(getattr(self, "flowstate", 0.0))
            min_store = max(0.05, min(0.90, min_store - 0.05 * fs))
        except Exception:
            pass
        if imp < min_store:
            try:
                hard_min = float(getattr(settings, "MEMORY_ALWAYS_STORE_MIN_SALIENCE", 0.80))
            except Exception:
                hard_min = 0.80
            if imp < hard_min:
                return
        try:
            max_len = int(getattr(settings, "MEM_MAX_TEXT_LEN", 1000))
        except Exception:
            max_len = 1000
        text_n = re.sub(r"\s+", " ", text or "").strip()
        if len(text_n) > max_len:
            text_n = text_n[:max_len]
        try:
            emb = emb_opt or await asyncio.wait_for(get_embedding(text_n), timeout=15.0)
        except asyncio.TimeoutError:
            emb = b""
        if (not emb) or (isinstance(emb, (bytes, bytearray)) and not any(emb)):
            return
        state_slice = {
            "arousal": self.state.get("arousal", 0.0),
            "valence": self.state.get("valence", 0.0),
            "stress":  self.state.get("stress",  0.0),
        }
        if uid_cur is not None:
            try:
                rec = self.attachments.get(uid_cur)
                if isinstance(rec, dict):
                    state_slice["attachment"] = float(rec.get("value", 0.0))
            except Exception:
                pass
        v_raw = readings.get("valence", 0.0)
        val01 = (v_raw + 1.0) * 0.5
        val01 = max(0.0, min(1.0, val01))
        await self.enhanced_memory.record(
            text=text_n,
            embedding=emb,
            emotions = {e: (val01 if e=="valence" else readings.get(e, 0.0)) for e in PRIMARY_EMOTIONS},
            state_metrics=state_slice,
            uid=uid_cur,
            salience=imp,
        )
        try:
            if uid_cur is not None and getattr(self, "ltm", None):
                self.spawn_coro(self.ltm.maybe_prune, uid_cur, name="ltm-maybe-prune")
        except Exception:
            logger.debug("schedule maybe_prune failed", exc_info=True)

    async def _consumer(worker_id: int):
        while True:
            item = await self._bg_queue.get()
            try:
                text, readings, emb_opt, imp, uid_cur = item
            except Exception:
                self._bg_queue.task_done()
                continue
            if text is None and readings is None:
                self._bg_queue.task_done()
                break
            try:
                await _process_bg_item(text, readings, emb_opt, imp, uid_cur)
            except Exception:
                logger.exception("BG-consumer[%s]: Error processing task", worker_id)
            finally:
                self._bg_queue.task_done()

    workers = [asyncio.create_task(_consumer(i), name=f"bg-consumer-{i}") for i in range(n)]
    try:
        await asyncio.gather(*workers)
    except asyncio.CancelledError:
        for t in workers:
            t.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        logger.info("BG-workers cancelled gracefully")
        raise
