#app/emo_engine/persona/utils/emotion_math.py
from __future__ import annotations

import logging

from functools import lru_cache
from math import exp, sqrt
from typing import Dict, List

from app.config import settings
from ..constants.emotions import (
    ALL_METRICS, COGNITIVE_METRICS,
    DRIVE_METRICS, DYAD_KEYS, PRIMARY_COORDS,
    PRIMARY_EMOTIONS, SECONDARY_EMOTIONS, SECONDARY_KEYS,
    SOCIAL_METRICS, STYLE_METRICS, TERTIARY_EMOTIONS,
    TERTIARY_KEYS, TRIAD_KEYS, VALID_DYADS,VALID_TRIADS,
    OPPOSITES, NON_DYNAMIC_METRICS,
)

logger = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:
    np = None


EMO_DT_DEFAULT = 0.10

_EMO_DT_PRIMARY: dict[str, float] = {
    "joy": 0.30,
    "anger": 0.30,
    "surprise": 0.25,
    "anticipation": 0.25,
    "trust": 0.25,
    "fear": 0.20,
    "stress": 0.20,
    "anxiety": 0.20,
    "energy": 0.25,
    "fatigue": 0.25,
    "sadness": 0.15,
    "disgust": 0.15,
}

_EMO_DT_SECONDARY: dict[str, float] = {
    **{
        sec: _EMO_DT_PRIMARY.get(prim, 0.20) * 0.8
        for prim, subs in SECONDARY_EMOTIONS.items()
        for sec in subs
    }
}

_EMO_DT_TERTIARY: dict[str, float] = {
    **{
        ter: _EMO_DT_SECONDARY.get(sec, 0.16) * 0.75
        for sec, subs in TERTIARY_EMOTIONS.items()
        for ter in subs
    }
}

_EMO_DT_STYLE: dict[str, float] = {**{m: 0.16 for m in STYLE_METRICS}, **{m: 0.17 for m in COGNITIVE_METRICS}}

_EMO_DT_DRIVE_SOCIAL: dict[str, float] = {
    **{m: 0.18 for m in DRIVE_METRICS},
    **{m: 0.18 for m in SOCIAL_METRICS},
}

EMO_DT_BASE: dict[str, float] = {}
for _m in ALL_METRICS:
    EMO_DT_BASE[_m] = (
        _EMO_DT_PRIMARY.get(_m)
        or _EMO_DT_SECONDARY.get(_m)
        or _EMO_DT_TERTIARY.get(_m)
        or _EMO_DT_STYLE.get(_m)
        or _EMO_DT_DRIVE_SOCIAL.get(_m)
        or EMO_DT_DEFAULT
    )


for _m in NON_DYNAMIC_METRICS:
    EMO_DT_BASE[_m] = 0.0

EMO_MATRIX_A: List[List[float]] | None = None
EMO_MATRIX_B: List[List[float]] | None = None
DYAD_MATRIX: List[List[float]] | None = None
TRIAD_MATRIX: List[List[float]] | None = None

_N = len(ALL_METRICS)


@lru_cache(maxsize=1)
def _init_matrices() -> None:

    global EMO_MATRIX_A, EMO_MATRIX_B, DYAD_MATRIX, TRIAD_MATRIX
    if EMO_MATRIX_A is not None:
        return

    EMO_MATRIX_A = [[-1.0 if i == j else 0.0 for j in range(_N)] for i in range(_N)]
    EMO_MATRIX_B = [[1.0 if i == j else 0.0 for j in range(_N)] for i in range(_N)]

    DYAD_MATRIX = [[0.0] * len(PRIMARY_EMOTIONS) for _ in DYAD_KEYS]
    TRIAD_MATRIX = [[0.0] * len(PRIMARY_EMOTIONS) for _ in TRIAD_KEYS]

    if np is not None:
        prim_vec = np.array([PRIMARY_COORDS[p] for p in PRIMARY_EMOTIONS])  # shape (12, 2)

        pairs_idx = np.array([[PRIMARY_EMOTIONS.index(a), PRIMARY_EMOTIONS.index(b)] for (a, b) in VALID_DYADS])
        dyad_vec = prim_vec[pairs_idx].sum(axis=1)
        dyad_unit = dyad_vec / (np.linalg.norm(dyad_vec, axis=1, keepdims=True) + 1e-9)
        DYAD_MATRIX[:] = (dyad_unit @ prim_vec.T).tolist()

        triad_idx = np.array(
            [[PRIMARY_EMOTIONS.index(a), PRIMARY_EMOTIONS.index(b), PRIMARY_EMOTIONS.index(c)] for (a, b, c) in VALID_TRIADS]
        )
        triad_vec = prim_vec[triad_idx].sum(axis=1)
        triad_unit = triad_vec / (np.linalg.norm(triad_vec, axis=1, keepdims=True) + 1e-9)
        TRIAD_MATRIX[:] = (triad_unit @ prim_vec.T).tolist()
        return

    for idx, (e1, e2) in enumerate(VALID_DYADS):
        x1, y1 = PRIMARY_COORDS[e1]
        x2, y2 = PRIMARY_COORDS[e2]
        bx, by = x1 + x2, y1 + y2
        norm = sqrt(bx * bx + by * by) or 1.0
        bx, by = bx / norm, by / norm
        for j, prim in enumerate(PRIMARY_EMOTIONS):
            px, py = PRIMARY_COORDS[prim]
            DYAD_MATRIX[idx][j] = bx * px + by * py

    for idx, (e1, e2, e3) in enumerate(VALID_TRIADS):
        bx = PRIMARY_COORDS[e1][0] + PRIMARY_COORDS[e2][0] + PRIMARY_COORDS[e3][0]
        by = PRIMARY_COORDS[e1][1] + PRIMARY_COORDS[e2][1] + PRIMARY_COORDS[e3][1]
        norm = sqrt(bx * bx + by * by) or 1.0
        bx, by = bx / norm, by / norm
        for j, prim in enumerate(PRIMARY_EMOTIONS):
            px, py = PRIMARY_COORDS[prim]
            TRIAD_MATRIX[idx][j] = bx * px + by * py


_init_matrices()


def _sigmoid(x: float, k: float = 10, mid: float = 0.5) -> float:

    z = k * (x - mid)
    if z >= 0:
        ez = exp(-z)
        return 1.0 / (1.0 + ez)
    ez = exp(z)
    return ez / (1.0 + ez)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:

    if isinstance(x, complex):
        x = x.real
    try:
        x = float(x)
    except Exception:
        logger.warning("emotion_math._clamp: bad value %r, fallback=%s", x, lo)
        x = lo
    return max(lo, min(hi, x))


def compute_dyad(e1: str, e2: str, state: Dict[str, float]) -> float:

    if e1 not in PRIMARY_COORDS or e2 not in PRIMARY_COORDS:
        logger.warning("compute_dyad: unknown coords for %s/%s", e1, e2)
        return 0.0

    v1, v2 = max(0.0, state.get(e1, 0.0)), max(0.0, state.get(e2, 0.0))
    if v1 == 0.0 or v2 == 0.0:
        return 0.0

    x1, y1 = PRIMARY_COORDS[e1]
    x2, y2 = PRIMARY_COORDS[e2]
    vx, vy = v1 * x1 + v2 * x2, v1 * y1 + v2 * y2

    bx, by = x1 + x2, y1 + y2
    norm_b = sqrt(bx * bx + by * by) or 1.0
    bx, by = bx / norm_b, by / norm_b

    proj_len = vx * bx + vy * by
    max_len = v1 + v2
    base_score = proj_len / max_len if max_len else 0.0

    harm = (v1 * v2) / (v1 + v2 + 1e-6) * 2

    return max(0.0, min(1.0, base_score * harm))


def _compute_secondary(self) -> None:

    energy = self.state.get("energy", 0.5)
    dynamic_thresh = settings.SECONDARY_THRESH * (1 + 0.5*(0.5-energy))
    beta = settings.SECONDARY_EMO_BETA * (1 - 0.3 * (energy - 0.5))

    raw_sec: Dict[str, float] = {}
    for (e1, e2), dyad_name in VALID_DYADS.items():
        v1 = self.state.get(e1, 0.0)
        v2 = self.state.get(e2, 0.0)
        if v1 > dynamic_thresh and v2 > dynamic_thresh:
            base_val = compute_dyad(e1, e2, self.state)
            jitter   = self._rng.uniform(-0.01, 0.01)
            raw_sec[dyad_name] = self._clamp(base_val + jitter)

    for name, val in list(raw_sec.items()):
        opp = OPPOSITES.get(name)
        if opp:
            raw_sec[opp] = self._clamp(raw_sec.get(opp, 0.0) * (1 - val))

    for name, val in raw_sec.items():
        prev = self.state.get(name, 0.0)
        self.state[name] = self._clamp(prev * beta + val * (1 - beta))

    for sec_key in SECONDARY_KEYS:
        if sec_key not in raw_sec and self.state.get(sec_key, 0.0) > 0.0:
            self.state[sec_key] *= beta

    for prim, subs in SECONDARY_EMOTIONS.items():
        for sec_name, fn in subs.items():
            if sec_name not in raw_sec:
                val = fn(self.state) + self._rng.uniform(-0.01, 0.01)
                if val > settings.SECONDARY_THRESH:
                    prev = self.state.get(sec_name, 0.0)
                    self.state[sec_name] = self._clamp(prev * beta + val * (1 - beta))


def _compute_tertiary(self) -> None:

    energy = self.state.get("energy", 0.5)
    dynamic_t3 = settings.TERTIARY_THRESH * (1 + 0.5*(0.5-energy))
    beta_t = settings.TERTIARY_EMO_BETA * (1 - 0.2 * (energy - 0.5))

    raw_ter: Dict[str, float] = {}
    for sec, subs in TERTIARY_EMOTIONS.items():
        for ter, fn in subs.items():
            base_val = fn(self.state)
            jitter = self._rng.uniform(-0.01, 0.01) if base_val > settings.TERTIARY_THRESH else 0.0
            val = self._clamp(base_val + jitter)
            raw_ter[ter] = self._clamp(val)

    for (e1, e2, e3), triad_name in VALID_TRIADS.items():
        v1, v2, v3 = (
            self.state.get(e1, 0.0),
            self.state.get(e2, 0.0),
            self.state.get(e3, 0.0),
        )
        if v1 > dynamic_t3 and v2 > dynamic_t3 and v3 > dynamic_t3:
            val = (v1 + v2 + v3) / 3 + self._rng.uniform(-0.01, 0.01)
            raw_ter[triad_name] = self._clamp(val)

    for name, val in raw_ter.items():
        prev = self.state.get(name, 0.0)
        self.state[name] = self._clamp(prev * beta_t + val * (1 - beta_t))

    for ter_key in TERTIARY_KEYS:
        if ter_key not in raw_ter and self.state.get(ter_key, 0.0) > 0.0:
            self.state[ter_key] *= beta_t

    for m in ALL_METRICS:
        lo, hi = (-1.0, 1.0) if m == "valence" else (0.0, 1.0)
        self.state[m] = self._clamp(self.state[m], lo, hi)


def suppress_opposite(metric: str, state: Dict[str, float]) -> None:
    opp = OPPOSITES.get(metric)
    if opp and opp in state:
        weight = _sigmoid(1 - state[metric], k=10, mid=0.5)
        state[opp] *= weight
