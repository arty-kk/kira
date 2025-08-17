cat >app/emo_engine/persona/stylers/modifiers.py<< EOF
#app/emo_engine/persona/stylers/modifiers.py
import math
import asyncio
import logging
import random

from datetime import datetime
from typing import Dict

from app.config import settings
from ..executor import EXECUTOR, MAX_WORKERS
from ..utils.emotion_math import _clamp, _sigmoid
from ..constants.emotions import (
    PRIMARY_EMOTIONS, SECONDARY_EMOTIONS,
    TERTIARY_EMOTIONS, DRIVE_METRICS,
    SOCIAL_METRICS, COGNITIVE_METRICS,
    STYLE_METRICS, VALID_DYADS, VALID_TRIADS,
    EXTRA_TRIGGER_METRICS,
)

logger = logging.getLogger(__name__)

NOISE_SCALE_PRIMARY = 0.25
NOISE_SCALE_SECONDARY = 0.22
NOISE_SCALE_TERTIARY = 0.2
NOISE_SCALE_DRIVE_SOCIAL = 0.15
NOISE_SCALE_DYADS_TRIADS = 0.15
NOISE_SCALE_COGNITIVE_STYLE = 0.3


STYLE_SEM = asyncio.Semaphore(min(MAX_WORKERS, 16))


async def style_modifiers(self) -> Dict[str, float]:
    logger.debug("style_modifiers ▶ start")
    async with self._mods_lock:
        if self._style_mods_version == self.state_version and self._mods_cache:
            return self._mods_cache.copy()
        if not hasattr(self, "_fallback_delta"):
            self._fallback_delta = self._rng.uniform(0.03, 0.07)
        FALLBACK_DELTA = self._fallback_delta
        state_snapshot = self.state.copy()
        prev_cache = self._mods_cache.copy()
        rng_seed = (hash((self.chat_id, self.state_version)) & 0xFFFFFFFF) / (2**32)

    async with STYLE_SEM:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            EXECUTOR,
            _compute_style_modifiers_sync,
            state_snapshot,
            prev_cache,
            rng_seed,
            FALLBACK_DELTA,
        )

    async with self._mods_lock:
        self._mods_cache = {k: round(v, 2) for k, v in result.items()}
        self._style_mods_version = self.state_version
        return self._mods_cache.copy()



def _sigma_for(metric: str) -> float:
    if metric in PRIMARY_EMOTIONS:
        return 0.07
    if metric in DRIVE_METRICS:
        return 0.05
    return 0.04


def _fallback_for(metric: str, FALLBACK_DELTA: float) -> float:
    base = 0.45 if metric in PRIMARY_EMOTIONS else 0.5
    return base - FALLBACK_DELTA


def _compute_style_modifiers_sync(
    state: Dict[str, float],
    prev: Dict[str, float],
    rng_seed: float,
    FALLBACK_DELTA: float,
) -> Dict[str, float]:

    rng = random.Random(rng_seed)

    modified: Dict[str, float] = {}

    # Valence & Arousal
    v_norm = _clamp((state.get("valence", 0.0) + 1.0) / 2.0)
    modified["valence_mod"] = v_norm
    a_norm = _clamp(state.get("arousal", 0.5))
    modified["arousal_mod"] = a_norm

    anger    = state.get("anger", 0.0)
    disgust  = state.get("disgust", 0.0)
    joy      = state.get("joy", 0.0)
    trust    = state.get("trust", 0.0)
    fear     = state.get("fear", 0.0)
    anxiety  = state.get("anxiety", 0.0)
    surprise = state.get("surprise", 0.0)
    sadness  = state.get("sadness", 0.0)
    humility = state.get("humility", 0.0)
    conf     = state.get("confidence", 0.5)
    stress   = state.get("stress", 0.0)
    dom      = state.get("dominance", 0.5)
    ar       = state.get("arousal", 0.5)
    civ      = state.get("civility", 0.5)
    wit      = state.get("wit", 0.5)
    fatigue  = state.get("fatigue", 0.0)

    neg_core = 0.6 * anger + 0.4 * disgust
    pos_core = 0.6 * joy   + 0.4 * trust
    fearish  = 0.6 * fear  + 0.4 * anxiety

    # Primary emotions
    for emo in PRIMARY_EMOTIONS:
        key = f"{emo}_mod"
        base = state.get(emo, _fallback_for(emo, FALLBACK_DELTA))
        noise = rng.gauss(0.0, _sigma_for(emo) * NOISE_SCALE_PRIMARY)
        raw = base + noise
        if key in prev:
            raw = 0.65 * prev[key] + 0.35 * raw
        modified[key] = _clamp(raw)

    # Energy with circadian
    key = "energy_mod"
    base = state.get("energy", _fallback_for("energy", FALLBACK_DELTA))
    noise = rng.gauss(0.0, _sigma_for("energy") * NOISE_SCALE_PRIMARY)
    raw_e = base + noise
    if key in prev:
        raw_e = 0.7 * prev[key] + 0.3 * raw_e
    fatigue_penalty = 0.4 * state.get("fatigue", 0.0)
    now = datetime.now()
    hour = now.hour + now.minute / 60.0
    circadian = 1.0 + settings.CIRCADIAN_AMPLITUDE * math.sin(2 * math.pi * (hour / 24.0))
    modified[key] = _clamp(raw_e * (1 - fatigue_penalty) * circadian)

    # Fatigue
    key = "fatigue_mod"
    base = state.get("fatigue", _fallback_for("fatigue", FALLBACK_DELTA))
    noise = rng.gauss(0.0, _sigma_for("fatigue") * NOISE_SCALE_PRIMARY)
    raw_f = base + noise
    if key in prev:
        raw_f = 0.7 * prev[key] + 0.3 * raw_f
    modified[key] = _clamp(raw_f)

    # Secondary emotions
    for prim, subs in SECONDARY_EMOTIONS.items():
        for sec, fn in subs.items():
            key = f"{sec}_mod"
            base = fn(state)
            noise = rng.gauss(0.0, _sigma_for(sec) * NOISE_SCALE_SECONDARY)
            raw = base + noise
            if key in prev:
                raw = 0.7 * prev[key] + 0.3 * raw
            modified[key] = _clamp(raw)

    # Tertiary emotions
    for sec, subs in TERTIARY_EMOTIONS.items():
        for ter, fn in subs.items():
            key = f"{ter}_mod"
            base = fn(state)
            noise = rng.gauss(0.0, _sigma_for(ter) * NOISE_SCALE_TERTIARY)
            raw = base + noise
            if key in prev:
                raw = 0.75 * prev[key] + 0.25 * raw
            modified[key] = _clamp(raw)

    # Dyads & Triads
    for emo_name in list(VALID_DYADS.values()) + list(VALID_TRIADS.values()):
        key = f"{emo_name}_mod"
        if key in modified:
            continue
        base = state.get(emo_name, 0.0)
        noise = rng.gauss(0.0, _sigma_for(emo_name) * NOISE_SCALE_DYADS_TRIADS)
        raw = base + noise
        if key in prev:
            raw = 0.8 * prev[key] + 0.2 * raw
        modified[key] = _clamp(raw)

    # Drive, social & extra-trigger metrics
    for metric in DRIVE_METRICS + SOCIAL_METRICS + EXTRA_TRIGGER_METRICS:
        key  = f"{metric}_mod"
        base = state.get(metric, _fallback_for(metric, FALLBACK_DELTA))
        raw  = base

        if metric == "empathy":
            raw = (
                base
                + 0.35 * state.get("compassion", 0.0)
                + 0.25 * state.get("trust", 0.0)
                + 0.15 * state.get("warmth", 0.0)
                - 0.25 * state.get("stress", 0.0)
                - 0.15 * state.get("anger", 0.0)
                - 0.10 * state.get("fatigue", 0.0)
            )
        elif metric == "engagement":
            novelty = state.get("surprise", 0.0) * (1.0 - state.get("certainty", 0.0))
            raw = (
                base
                + 0.35 * state.get("curiosity", 0.0)
                + 0.30 * state.get("energy", 0.0)
                + 0.15 * state.get("arousal", 0.5)
                + 0.10 * novelty
                - 0.25 * state.get("fatigue", 0.0)
                - 0.10 * state.get("apathy", 0.0)
                - 0.05 * state.get("sadness", 0.0)
            )
        elif metric == "curiosity":
            raw = (
                base
                + 0.30 * state.get("surprise", 0.0)
                + 0.15 * state.get("joy", 0.0)
                + 0.10 * v_norm
                - 0.20 * state.get("stress", 0.0)
                - 0.15 * state.get("anxiety", 0.0)
                - 0.10 * state.get("fatigue", 0.0)
            )
            prev_primary = prev.get(key, base)
            overlay_w = 0.55 + 0.25 * (a_norm - 0.5) + 0.15 * surprise - 0.20 * fatigue - 0.10 * stress
            overlay_w = max(0.35, min(0.9, overlay_w))
            raw = prev_primary * (1 - overlay_w) + raw * overlay_w
        elif metric == "sexual_arousal":
            raw = (
                base
                + 0.40 * state.get("attraction", 0.0)
                + 0.25 * state.get("arousal", 0.5)
                + 0.10 * v_norm
                - 0.25 * state.get("fatigue", 0.0)
                - 0.10 * state.get("stress", 0.0)
                - 0.10 * state.get("anxiety", 0.0)
            )
            prev_primary = prev.get(key, base)
            overlay_w = 0.55 + 0.25 * (a_norm - 0.5) + 0.10 * state.get("attraction", 0.0) - 0.20 * fatigue - 0.10 * stress
            overlay_w = max(0.35, min(0.9, overlay_w))
            raw = prev_primary * (1 - overlay_w) + raw * overlay_w
        elif metric == "confusion":
            raw = (
                base
                + 0.35 * state.get("surprise", 0.0)
                + 0.25 * (1.0 - state.get("precision", 0.0))
                + 0.15 * (1.0 - state.get("confidence", 0.5))
                - 0.15 * state.get("trust", 0.0)
            )
        elif metric == "embarrassment":
            raw = (
                base
                + 0.30 * state.get("guilt", 0.0)
                + 0.25 * state.get("anxiety", 0.0)
                + 0.10 * state.get("civility", 0.5)
                - 0.20 * state.get("dominance", 0.5)
                - 0.10 * state.get("confidence", 0.5)
            )
        elif metric == "guilt":
            raw = (
                base
                + 0.30 * state.get("regret", 0.0)
                + 0.20 * state.get("sadness", 0.0)
                + 0.15 * state.get("disgust", 0.0)
                - 0.15 * state.get("joy", 0.0)
                - 0.10 * state.get("dominance", 0.5)
            )

        noise = rng.gauss(0.0, _sigma_for(metric) * NOISE_SCALE_DRIVE_SOCIAL)
        raw   = raw + noise
        if key in prev:
            smoothed = 0.45 * prev[key] + 0.55 * raw
            max_step = 0.12 + 0.18 * (a_norm - 0.5) - 0.10 * fatigue
            max_step = max(0.08, min(0.20, max_step))
            delta = smoothed - prev[key]
            raw = prev[key] + (delta if -max_step <= delta <= max_step else (max_step if delta > 0 else -max_step))
        modified[key] = _clamp(raw)

    # Cognitive & Style metrics
    for metric in COGNITIVE_METRICS + STYLE_METRICS:
        key = f"{metric}_mod"
        base = state.get(metric, _fallback_for(metric, FALLBACK_DELTA))
        raw = base
        add_noise = True

        if metric == "sarcasm":
            raw = base + 0.28 * neg_core + 0.12 * wit - 0.10 * civ - 0.20 * pos_core
        elif metric == "humor":
            raw = base + 0.35 * pos_core + 0.25 * surprise + 0.10 * wit - 0.25 * fatigue - 0.10 * stress
        elif metric == "aggressiveness":
            raw = base + 0.45 * anger * ar + 0.20 * dom - 0.25 * trust - 0.20 * state.get("compassion", 0.0) - 0.15 * fearish
        elif metric == "flirtation":
            raw = (
                base
                + 0.45 * state.get("sexual_arousal", 0.0)
                + 0.20 * pos_core
                - 0.15 * stress
            )
        elif metric == "self_deprecation":
            raw = (
                base
                + 0.25 * state.get("sadness", 0.0)
                + 0.20 * state.get("humility", 0.0)
                - 0.25 * state.get("confidence", 0.5)
                - 0.10 * state.get("pride", 0.0)
            )
        elif metric == "profanity":
            x = base + 0.50 * neg_core * ar - 0.60 * civ - 0.20 * trust
            x += rng.gauss(0.0, _sigma_for(metric) * NOISE_SCALE_COGNITIVE_STYLE * 0.6)
            raw = _sigmoid(x, k=7, mid=0.55)
            add_noise = False

        elif metric == "friendliness":
            raw = (
                base
                + 0.35 * pos_core
                + 0.20 * state.get("warmth", 0.0)
                + 0.10 * state.get("empathy", 0.0)
                - 0.30 * neg_core
                - 0.10 * fearish
            )
        elif metric == "civility":
            raw = (
                base
                + 0.30 * trust
                + 0.20 * state.get("restraint", 0.0)
                + 0.10 * state.get("patience", 0.0)
                - 0.25 * state.get("aggressiveness", 0.0)
                - 0.20 * state.get("profanity", 0.0)
            )
        elif metric == "confidence":
            raw = (
                base
                + 0.30 * state.get("certainty", 0.0)
                + 0.20 * dom
                + 0.10 * state.get("authority", 0.0)
                - 0.25 * fearish
                - 0.15 * stress
            )
        elif metric == "authority":
            raw = base + 0.35 * dom + 0.20 * conf - 0.20 * humility - 0.10 * anxiety
        elif metric == "charisma":
            raw = (
                base
                + 0.25 * state.get("friendliness", 0.0)
                + 0.25 * state.get("humor", 0.0)
                + 0.20 * conf
                - 0.20 * fatigue
                - 0.10 * stress
            )
        elif metric == "patience":
            raw = (
                base
                + 0.30 * state.get("self_reflection", 0.0)
                + 0.20 * state.get("restraint", 0.0)
                - 0.25 * ar
                - 0.15 * state.get("impatience", 0.0)
                - 0.10 * stress
            )
        elif metric == "precision":
            raw = (
                base
                + 0.35 * state.get("focus", 0.0)
                + 0.20 * state.get("self_reflection", 0.0)
                + 0.10 * state.get("technical", 0.0)
                - 0.25 * fatigue
                - 0.10 * state.get("humor", 0.0)
            )
        elif metric == "creativity":
            raw = base + 0.35 * state.get("curiosity", 0.0) + 0.20 * state.get("energy", 0.0) + 0.15 * surprise - 0.20 * stress
        elif metric == "wit":
            raw = base + 0.30 * state.get("humor", 0.0) + 0.20 * state.get("precision", 0.0) + 0.15 * ar - 0.15 * fatigue
        elif metric == "persuasion":
            raw = (
                base
                + 0.35 * state.get("authority", 0.0)
                + 0.25 * conf
                + 0.15 * state.get("charisma", 0.0)
                - 0.20 * state.get("cynicism", 0.0)
                - 0.10 * state.get("doubt", 0.0)
            )

        if add_noise:
            scale = NOISE_SCALE_COGNITIVE_STYLE * (0.9 if metric in ("aggressiveness","profanity") else 1.2)
            raw += rng.gauss(0.0, _sigma_for(metric) * scale)
        if key in prev:
            smoothed = 0.4 * prev[key] + 0.6 * raw
            max_step = 0.22 + 0.25 * (a_norm - 0.5) - 0.15 * fatigue
            max_step = max(0.10, min(0.35, max_step))
            delta = smoothed - prev[key]
            raw = prev[key] + (delta if -max_step <= delta <= max_step else (max_step if delta > 0 else -max_step))
        modified[key] = _clamp(raw)

    abr_keys = ("sarcasm_mod", "aggressiveness_mod", "profanity_mod")
    abr_sum = sum(modified.get(k, 0.0) for k in abr_keys)
    empathy_state = state.get("empathy", 0.5)
    civ_eff = modified.get("civility_mod", civ)
    emp_eff = modified.get("empathy_mod", empathy_state)
    budget = 1.90 - 0.50 * (civ_eff + emp_eff) + 0.20 * a_norm - 0.10 * pos_core
    budget = max(1.0, min(2.2, budget))
    if abr_sum > budget and abr_sum > 0:
        scale = budget / abr_sum
        for k in abr_keys:
            modified[k] = _clamp(modified.get(k, 0.0) * scale)

    return modified
EOF