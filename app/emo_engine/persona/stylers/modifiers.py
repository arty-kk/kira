cat >app/emo_engine/persona/stylers/modifiers.py<< EOF
#app/emo_engine/persona/stylers/modifiers.py
import threading

from typing import Dict, Optional

from ..utils.emotion_math import _clamp, _sigmoid
from ..constants.emotions import (
    PRIMARY_EMOTIONS, SECONDARY_EMOTIONS,
    TERTIARY_EMOTIONS, DRIVE_METRICS,
    SOCIAL_METRICS, COGNITIVE_METRICS,
    STYLE_METRICS, VALID_DYADS, VALID_TRIADS,
    EXTRA_TRIGGER_METRICS,
)

def style_modifiers(self) -> Dict[str, float]:

    lock = getattr(self, "_mods_lock", None)
    if lock is None:
        self._mods_lock = threading.Lock()
        lock = self._mods_lock

    with lock:
        if self._style_mods_version == self.state_version:
            return self._mods_cache.copy()
        self._style_mods_version = self.state_version
        
        if not hasattr(self, "_fallback_delta"):
            self._fallback_delta = self._rng.uniform(0.03, 0.07)
        FALLBACK_DELTA = self._fallback_delta

        DEFAULT_BASE = {m: 0.45 for m in PRIMARY_EMOTIONS}
        DEFAULT_BASE.update({"aggressiveness": 0.3, "friendliness": 0.6})
        def _fallback(metric: str) -> float:
            return DEFAULT_BASE.get(metric, 0.5 - FALLBACK_DELTA)

        def _sigma_for(metric: str) -> float:
            if metric in PRIMARY_EMOTIONS:
                return 0.07
            if metric in DRIVE_METRICS:
                return 0.05
            return 0.04

        modified: Dict[str, float] = {}

        valence_norm = _clamp((self.state.get("valence", 0.0) + 1.0) / 2.0)
        modified["valence_mod"] = valence_norm
        self.state["valence_mod"] = valence_norm

        arousal_norm = _clamp(self.state.get("arousal", 0.5))
        modified["arousal_mod"] = arousal_norm
        self.state["arousal_mod"] = arousal_norm


        for emo in PRIMARY_EMOTIONS:
            key_mod = f"{emo}_mod"
            prev = self._mods_cache.get(key_mod)
            base = self.state.get(emo, _fallback(emo))
            noise = self._rng.gauss(0.0, _sigma_for(emo))
            raw = base + noise
            if prev is not None:
                raw = 0.7 * prev + 0.3 * raw
            val = _clamp(raw)
            modified[key_mod] = val
            self.state[key_mod] = val

        prev_e = self._mods_cache.get("energy_mod")
        base = self.state.get("energy", _fallback("energy"))
        noise = self._rng.gauss(0.0, _sigma_for("energy"))
        raw_e = base + noise
        if prev_e is not None:
            raw_e = 0.6 * prev_e + 0.4 * raw_e
        fatigue_penalty = 0.4 * self.state.get("fatigue", 0.0)
        val_e = _clamp(raw_e * (1 - fatigue_penalty))
        modified["energy_mod"] = val_e
        self.state["energy_mod"] = val_e

        prev_f = self._mods_cache.get("fatigue_mod")
        base = self.state.get("fatigue", _fallback("fatigue"))
        noise = self._rng.gauss(0.0, _sigma_for("fatigue"))
        raw_f = base + noise
        if prev_f is not None:
            raw_f = 0.6 * prev_f + 0.4 * raw_f
        val_f = _clamp(raw_f)
        modified["fatigue_mod"] = val_f
        self.state["fatigue_mod"] = val_f

        for metric in ("friendliness", "aggressiveness", "confidence"):
            key_mod = f"{metric}_mod"
            prev = self._mods_cache.get(key_mod)
            base = self.state.get(metric, _fallback(metric))
            sigma = _sigma_for(metric)
            noise = self._rng.gauss(0.0, sigma * 0.7)
            raw = base + noise
            if prev is not None:
                raw = 0.7 * prev + 0.3 * raw
            val = _clamp(raw)
            modified[key_mod] = val
            self.state[key_mod] = val

        for prim, subs in SECONDARY_EMOTIONS.items():
            for sec, fn in subs.items():
                key_mod = f"{sec}_mod"
                prev = self._mods_cache.get(key_mod)
                base = fn(self.state)
                sigma = _sigma_for(sec)
                noise = self._rng.gauss(0.0, sigma * 0.6)
                raw = base + noise
                if prev is not None:
                    raw = 0.7 * prev + 0.3 * raw
                val = _clamp(raw)
                modified[key_mod] = val
                self.state[key_mod] = val

        for sec, subs in TERTIARY_EMOTIONS.items():
            for ter, fn in subs.items():
                key_mod = f"{ter}_mod"
                prev = self._mods_cache.get(key_mod)
                base = fn(self.state)
                sigma = _sigma_for(ter)
                noise = self._rng.gauss(0.0, sigma * 0.6)
                raw = base + noise
                if prev is not None:
                    raw = 0.7 * prev + 0.3 * raw
                val = _clamp(raw)
                modified[key_mod] = val
                self.state[key_mod] = val

        for metric in DRIVE_METRICS + SOCIAL_METRICS + EXTRA_TRIGGER_METRICS:
            key_mod = f"{metric}_mod"
            prev = self._mods_cache.get(key_mod)
            base = self.state.get(metric, _fallback(metric))
            sigma = _sigma_for(metric)
            noise = self._rng.gauss(0.0, sigma * 0.8)
            raw = base + noise
            if prev is not None:
                raw = 0.7 * prev + 0.3 * raw
            val = _clamp(raw)
            modified[key_mod] = val
            self.state[key_mod] = val

        for emo_name in list(VALID_DYADS.values()) + list(VALID_TRIADS.values()):
            key_mod = f"{emo_name}_mod"
            if key_mod in modified:
                continue
            prev = self._mods_cache.get(key_mod)
            base = self.state.get(emo_name, 0.0)
            sigma = _sigma_for(emo_name)
            noise = self._rng.gauss(0.0, sigma * 0.5)
            raw = base + noise
            if prev is not None:
                raw = 0.7 * prev + 0.3 * raw
            val = _clamp(raw)
            modified[key_mod] = val
            self.state[key_mod] = val

        for metric in COGNITIVE_METRICS + STYLE_METRICS:
            key_mod = f"{metric}_mod"
            prev = self._mods_cache.get(key_mod)
            base = self.state.get(metric, _fallback(metric))
            if metric == "flirtation":
                valence_norm = (self.state.get("valence", 0.0) + 1.0) / 2.0
                raw = valence_norm * self.state.get("sexual_arousal", 0.0)
            elif metric == "sarcasm":
                raw = base * (1.0 - abs(self.state.get("valence", 0.0) - 0.2))
            elif metric == "humor":
                raw = base * (1 - 0.5 * self.state.get("fatigue", 0.0))
            else:
                raw = base
            sigma = _sigma_for(metric)
            noise = self._rng.gauss(0.0, sigma * 0.6)
            raw = raw + noise
            if prev is not None:
                raw = 0.7 * prev + 0.3 * raw
            if metric == "profanity":
                raw = _sigmoid(raw, k=5, mid=0.35)
            val = _clamp(raw)
            modified[key_mod] = val
            self.state[key_mod] = val

        self._mods_cache = {k: round(v, 2) for k, v in modified.items()}

        return self._mods_cache.copy()
EOF