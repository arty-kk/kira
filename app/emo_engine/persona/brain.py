#app/emo_engine/persona/brain.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, MutableMapping, Iterable, Tuple, Optional

from .constants.emotions import (
    PRIMARY_EMOTIONS, SECONDARY_EMOTIONS,
    TERTIARY_EMOTIONS, SECONDARY_KEYS,
    TERTIARY_KEYS, VALID_DYADS,
    VALID_TRIADS, ALL_METRICS, FAT_CLAMP,
    make_learned_secondary,
)
from .constants.tone_map import (
    Tone, project_state_to_tones,
    apply_tone_to_state,
)

@dataclass
class PersonaBrainConfig:
    activation_threshold: float = 0.7
    learn_threshold: int = 20
    max_learned_metrics: int = 64
    coactivation_pool: Optional[Iterable[str]] = None

    def __post_init__(self) -> None:
        self.activation_threshold = float(
            max(0.0, min(1.0, self.activation_threshold))
        )
        if self.learn_threshold < 1:
            self.learn_threshold = 1
        if self.max_learned_metrics < 0:
            self.max_learned_metrics = 0


@dataclass
class PersonaBrain:

    config: PersonaBrainConfig = field(default_factory=PersonaBrainConfig)
    state: Dict[str, float] = field(default_factory=dict)
    coactivation_counts: Dict[frozenset, int] = field(default_factory=dict)
    learned_metrics: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.state:
            self.state = {name: 0.0 for name in ALL_METRICS}
        else:
            for name in ALL_METRICS:
                self.state.setdefault(name, 0.0)

        if self.config.coactivation_pool is None:
            self._coactivation_pool = list(dict.fromkeys(PRIMARY_EMOTIONS))
        else:
            self._coactivation_pool = [
                m for m in self.config.coactivation_pool if m in ALL_METRICS
            ]

        for name in SECONDARY_KEYS + TERTIARY_KEYS:
            self.state.setdefault(name, 0.0)
        for name in VALID_DYADS.values():
            self.state.setdefault(name, 0.0)
        for name in VALID_TRIADS.values():
            self.state.setdefault(name, 0.0)

        self.recompute_derived()

    def set_state_from_snapshot(self, snapshot: Mapping[str, float]) -> None:

        for name in ALL_METRICS:
            v = float(snapshot.get(name, self.state.get(name, 0.0)))
            if name == "valence":
                self.state[name] = max(-1.0, min(1.0, v))
            else:
                self.state[name] = FAT_CLAMP(v)

        for name in SECONDARY_KEYS + TERTIARY_KEYS:
            self.state.setdefault(name, 0.0)
        for name in VALID_DYADS.values():
            self.state.setdefault(name, 0.0)
        for name in VALID_TRIADS.values():
            self.state.setdefault(name, 0.0)

    def set_metric(self, name: str, value: float) -> None:
        v = float(value)
        if name == "valence":
            self.state[name] = max(-1.0, min(1.0, v))
        else:
            self.state[name] = FAT_CLAMP(v)

    def update_state(self, deltas: Mapping[str, float], *, mode: str = "add") -> None:

        if mode not in ("add", "set"):
            raise ValueError("mode must be 'add' or 'set'")

        for name, value in deltas.items():
            if mode == "set":
                self.set_metric(name, value)
            else:
                self.set_metric(name, self.state.get(name, 0.0) + value)

        self.recompute_derived()
        self._log_coactivations()
        self._maybe_learn_new_secondaries()


    def recompute_derived(self) -> None:

        s = self.state

        for _base, subs in SECONDARY_EMOTIONS.items():
            for name, fn in subs.items():
                try:
                    s[name] = FAT_CLAMP(float(fn(s)))
                except KeyError:
                    continue

        for (e1, e2), name in VALID_DYADS.items():
            v = 0.5 * (s.get(e1, 0.0) + s.get(e2, 0.0))
            s[name] = FAT_CLAMP(v)

        for (e1, e2, e3), name in VALID_TRIADS.items():
            v = (s.get(e1, 0.0) + s.get(e2, 0.0) + s.get(e3, 0.0)) / 3.0
            s[name] = FAT_CLAMP(v)

        for _base, subs in TERTIARY_EMOTIONS.items():
            for name, fn in subs.items():
                try:
                    s[name] = FAT_CLAMP(float(fn(s)))
                except KeyError:
                    continue


    def _log_coactivations(self) -> None:

        active = [
            m
            for m in self._coactivation_pool
            if self.state.get(m, 0.0) >= self.config.activation_threshold
        ]

        for i, a in enumerate(active):
            for b in active[i + 1 :]:
                key = frozenset((a, b))
                self.coactivation_counts[key] = self.coactivation_counts.get(key, 0) + 1

    def _maybe_learn_new_secondaries(self) -> None:
        if self.config.max_learned_metrics == 0:
            return
        if len(self.learned_metrics) >= self.config.max_learned_metrics:
            return

        threshold = self.config.learn_threshold

        for pair, count in list(self.coactivation_counts.items()):
            if count < threshold:
                continue

            a, b = sorted(pair)
            base_name = f"{a}_{b}_assoc"
            name = base_name
            suffix = 2

            while name in ALL_METRICS or name in self.learned_metrics:
                name = f"{base_name}_{suffix}"
                suffix += 1

            make_learned_secondary(name, {a: 0.5, b: 0.5})
            self.learned_metrics[name] = (a, b)

            self.state.setdefault(name, 0.0)
            self.coactivation_counts[pair] = 0

            if len(self.learned_metrics) >= self.config.max_learned_metrics:
                break


    def project_to_tones(self) -> Dict[Tone, float]:
        return project_state_to_tones(self.state)

    def nudge_tone(self, tone: Tone, intensity: float, *, lr: float = 0.1) -> None:
        apply_tone_to_state(self.state, tone, intensity, lr)
        self.recompute_derived()