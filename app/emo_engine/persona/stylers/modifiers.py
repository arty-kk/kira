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
        
        base_noise = 0.02 \
                   + 0.04 * self.state.get("arousal", 0.5) \
                   + 0.03 * self.state.get("stress", 0.5)
        fatigue = self.state.get("fatigue", 0.0)
        noise = base_noise * (1 - 0.5 * fatigue) \
              + 0.06 * self.state.get("energy", 0.5)

        modified: Dict[str, float] = {}

        valence_norm = _clamp((self.state.get("valence", 0.0) + 1.0) / 2.0)
        modified["valence_mod"] = valence_norm
        self.state["valence_mod"] = valence_norm

        arousal_norm = _clamp(self.state.get("arousal", 0.5))
        modified["arousal_mod"] = arousal_norm
        self.state["arousal_mod"] = arousal_norm


        for emo in PRIMARY_EMOTIONS:
            prev: Optional[float] = self._mods_cache.get(f"{emo}_mod")
            raw  = self.state.get(emo, 0.5) + self._rng.uniform(-noise, noise)
            if prev is not None:
                raw = 0.3 * raw + 0.7 * prev
            val = _clamp(raw)
            modified[f"{emo}_mod"] = val
            self.state[f"{emo}_mod"] = val

        prev_e = self._mods_cache.get("energy_mod")
        raw_energy = self.state.get("energy", 0.5) + self._rng.uniform(-noise, noise)
        if prev_e is not None:
            raw_energy = 0.4 * raw_energy + 0.6 * prev_e
        fatigue_penalty = 0.4 * self.state.get("fatigue", 0.0)
        val_energy = _clamp(raw_energy * (1 - fatigue_penalty))
        modified["energy_mod"] = val_energy
        self.state["energy_mod"] = val_energy

        prev_f = self._mods_cache.get("fatigue_mod")
        raw_fatigue = self.state.get("fatigue", 0.5) + self._rng.uniform(-noise, noise)
        if prev_f is not None:
            raw_fatigue = 0.4 * raw_fatigue + 0.6 * prev_f
        val_fatigue = _clamp(raw_fatigue)
        modified["fatigue_mod"] = val_fatigue
        self.state["fatigue_mod"] = val_fatigue

        for metric in ("friendliness", "aggressiveness", "confidence"):
            key_mod = f"{metric}_mod"
            if key_mod not in modified:
                val = _clamp(self.state.get(metric, 0.5) + self._rng.uniform(-noise*0.5, noise*0.5))
                modified[key_mod] = val
                self.state[key_mod] = val

        for prim, subs in SECONDARY_EMOTIONS.items():
            for sec, fn in subs.items():
                raw = fn(self.state) + self._rng.uniform(-noise, noise)
                val = _clamp(raw)
                modified[f"{sec}_mod"] = val
                self.state[f"{sec}_mod"] = val 

        for sec, subs in TERTIARY_EMOTIONS.items():
            for ter, fn in subs.items():
                raw = fn(self.state) + self._rng.uniform(-noise, noise)
                val = _clamp(raw)
                modified[f"{ter}_mod"] = val
                self.state[f"{ter}_mod"] = val 

        for metric in DRIVE_METRICS + SOCIAL_METRICS + EXTRA_TRIGGER_METRICS:
            raw = self.state.get(metric, 0.5) + self._rng.uniform(-noise * 1.2, noise * 1.2)
            val = _clamp(raw)
            modified[f"{metric}_mod"] = val
            self.state[f"{metric}_mod"] = val

        for emo_name in list(VALID_DYADS.values()) + list(VALID_TRIADS.values()):
            key_mod = f"{emo_name}_mod"
            if key_mod in modified:
                continue
            raw = self.state.get(emo_name, 0.0) + self._rng.uniform(-noise, noise)
            val = _clamp(raw)
            modified[key_mod] = val
            self.state[key_mod] = val

        for metric in COGNITIVE_METRICS + STYLE_METRICS:
                
            raw = self.state.get(metric, 0.5)
            
            if metric == "flirtation":
                valence_norm = (self.state.get("valence", 0.0) + 1.0) / 2.0
                raw = valence_norm * self.state.get("sexual_arousal", 0.0)
            
            elif metric == "sarcasm":
                raw *= (1.0 - abs(self.state.get("valence", 0.0) - 0.2))
            
            elif metric == "humor":
                raw *= (1 - 0.5 * self.state.get("fatigue", 0.0))
            
            elif metric == "precision":
                raw *= (1 - 0.3 * self.state.get("fatigue", 0.0))
            
            elif metric == "creativity":
                f = self.state.get("fatigue", 0.0)
                raw *= (1 - 0.4 * abs(f - 0.4))
            
            elif metric == "profanity":
                agg = self.state.get("aggressiveness", 0.0)
                strss = self.state.get("stress", 0.0)
                raw = (0.7 * agg + 0.3 * strss) * (1 + strss * 0.5)
            
            elif metric == "charisma":
                raw = (
                    0.5 * self.state.get("confidence_mod", self.state.get("confidence", 0.5))
                + 0.3 * self.state.get("friendliness_mod", self.state.get("friendliness", 0.5))
                + 0.2 * self.state.get("energy_mod", self.state.get("energy", 0.5))
                )
                raw *= (1 - 0.4 * self.state.get("fatigue", 0.0))
                raw *= (1 - 0.2 * self.state.get("anger_mod", self.state.get("anger", 0.0)))
            
            elif metric == "persuasion":
                raw = (
                    0.5 * self.state.get("charisma_mod", self.state.get("charisma", 0.5))
                + 0.3 * self.state.get("precision_mod", self.state.get("precision", 0.5))
                + 0.2 * self.state.get("confidence_mod", self.state.get("confidence", 0.5))
                )
                raw *= (1 - 0.25 * self.state.get("aggressiveness_mod", self.state.get("aggressiveness", 0.0)))
            
            elif metric == "authority":
                raw = (
                    0.6 * self.state.get("confidence_mod", self.state.get("confidence", 0.5))
                + 0.3 * self.state.get("precision_mod", self.state.get("precision", 0.5))
                + 0.1 * self.state.get("valence_mod", self.state.get("valence", 0.0))
                )
                raw *= (1 - 0.25 * self.state.get("aggressiveness_mod", self.state.get("aggressiveness", 0.0)))

            elif metric == "wit":
                raw = (
                    0.6 * self.state.get("humor_mod", self.state.get("humor", 0.5))
                + 0.3 * self.state.get("creativity_mod", self.state.get("creativity", 0.5))
                + 0.2 * self.state.get("energy_mod", self.state.get("energy",   0.5))
                )
                raw -= 0.25 * self.state.get("fatigue", 0.0)

            elif metric == "patience":
                raw = (0.5 * self.state.get("empathy_mod", self.state.get("empathy", 0.5))
                + 0.3 * (1 - self.state.get("stress_mod", self.state.get("stress", 0.5)))
                + 0.2 * (1 - self.state.get("anxiety_mod", self.state.get("anxiety", 0.5)))
                )

            raw += self._rng.uniform(-noise, noise)
            if metric == "profanity":
                raw = _sigmoid(raw, k=5, mid=0.35)
            val = _clamp(raw)
                
            modified[f"{metric}_mod"] = val
            self.state[f"{metric}_mod"] = val

        self._mods_cache = {k: round(v, 2) for k, v in modified.items()}

        return self._mods_cache.copy()
EOF