cat >app/emo_engine/persona/constants/emotional_state.py<< 'EOF'
#app/emo_engine/persona/constants/emotional_state.py
from __future__ import annotations

from typing import Dict, Optional, Tuple, List
from .tone_map import TONE_MAP, Tone


MIN_GLOBAL_THR = 0.25
TOP_K = 2


def compute_emotional_state(
    state: Dict[str, float],
    mods: Dict[str, float],
    dominant: Optional[str] = None,
    *,
    allow_mixed: bool = True,
) -> str:

    if dominant:
        return dominant

    v = float(state.get("valence", 0.0))
    a = float(state.get("arousal", 0.5))
    if abs(v) < 0.2 and a < 0.4:
        return "Neutral"

    scores: List[Tuple[Tone, float]] = []
    for key, tone in TONE_MAP.items():
        v = mods.get(key, state.get(key.replace("_mod", ""), 0.0))
        v = max(0.0, min(1.0, v))
        scores.append((tone, v))

    scores.sort(key=lambda kv: kv[1], reverse=True)
    if not scores:
        return "Neutral"

    top = scores[0][1]
    dyn_thr = max(MIN_GLOBAL_THR, top - 0.15)
    chosen = [(t, v) for t, v in scores if v >= dyn_thr][:TOP_K]

    if not chosen:
        return "Neutral"

    if not allow_mixed or len(chosen) == 1:
        return chosen[0][0].name

    total = sum(v for _, v in chosen) or 1.0
    parts = []
    acc = 0
    for i, (t, v) in enumerate(chosen):
        if i == len(chosen) - 1:
            pct = max(0, 100 - acc)
        else:
            pct = int(round(v / total * 100))
        parts.append(f"{t.name}:{pct}")
        acc += pct
    return "+".join(parts)
EOF