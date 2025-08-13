cat >app/emo_engine/persona/stylers/guidelines.py<< EOF
#app/emo_engine/persona/stylers/guidelines.py
from __future__ import annotations

import asyncio
import logging
import random
import math
import time
import secrets

from typing import Dict, List, Set, Tuple, Optional

from ..constants.extra_devices import EXTRA_DEVICES, BOT_SIGNATURES
from ..constants.tone_map import TONE_MAP
from ..constants.emotional_state import compute_emotional_state


logger = logging.getLogger(__name__)


def _metric(state: Dict[str, float], mods: Dict[str, float], key: str, fallback: float = 0.5) -> float:
    raw = mods.get(key)
    if raw is None:
        raw = state.get(key.replace("_mod", ""), fallback)
    return float(raw)


def _append_once(gl: list[str], flag: str) -> None:
    if flag not in gl:
        gl.append(flag)


def _set_flag(gl: list[str], prefix: str, flag: str) -> None:
    gl[:] = [f for f in gl if not f.startswith(prefix + "=") and not f.startswith(prefix + "+=")]
    gl.append(flag)


def _remove_conflicts(devices: List[str]) -> None:
    name_map = {d.name: d for d in EXTRA_DEVICES}
    to_remove: Set[str] = set()
    for name in devices:
        dev = name_map.get(name)
        if not dev:
            continue
        for other in dev.exclusive_with:
            if other in devices:
                to_remove.add(other)
    devices[:] = [d for d in devices if d not in to_remove]


def _compute_hostility(state: Dict[str, float], mods: Dict[str, float]) -> float:

    V        = state.get("valence", 0.0)
    neg      = max(0.0, -V)
    arousal  = float(mods.get("arousal_mod",  state.get("arousal",  0.5)))
    dom      = float(mods.get("dominance_mod", state.get("dominance", 0.33)))
    fatigue  = float(mods.get("fatigue_mod",  state.get("fatigue",  0.0)))

    h = neg * (0.5 + 0.5 * arousal) * (0.5 + 0.5 * dom) * (1.0 - 0.3 * fatigue)
    return max(0.0, min(1.0, h))


def _gather_extras(
    self,
    state: Dict[str, float],
    max_items: int,
    mods: Optional[Dict[str, float]] = None,
) -> List[str]:
    rng_seed = state.get("seed")
    rng = random.Random(rng_seed if rng_seed is not None else secrets.randbits(64))
    mods = mods or getattr(self, "_mods_cache", {})

    devices = list(EXTRA_DEVICES)
    rng.shuffle(devices)

    chosen: List[str] = []
    for device in devices:
        if device.should_apply(_metric(state, mods, device.metric_key), rng):
            chosen.append(device.name)
            _remove_conflicts(chosen)
            if len(chosen) >= max_items // 2:
                break
    return chosen


async def style_guidelines(
    self,
    uid: Optional[int] = None,
    max_items: int = 12,
    *,
    human_mode: bool = True,
) -> List[str]:

    state = self.state.copy()
    state["seed"] = (self.chat_id << 32) ^ self.state_version
    mods = self._mods_cache.copy()
    if not mods:
        try:
            mods = await asyncio.wait_for(self.style_modifiers(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "style_modifiers() timed-out; using previous cache (%d items)",
                len(self._mods_cache),
            )

    weight = self._decayed_weight(uid) if uid is not None else None
    last_msg = self._last_user_msg or ""

    now = time.time()
    if (now - getattr(self, "_mem_count_cache_ts", 1e9)) < 60:
        n = getattr(self, "_mem_count_cache", 0)
    else:
        try:
            n = await self.enhanced_memory.count_entries()
        except Exception:
            n = 0
        self._mem_count_cache = n
        self._mem_count_cache_ts = now

    prev_int = getattr(self, "_prev_intensity_pct", None)
    prev_addr = getattr(self, "_prev_address_score", None)

    snapshot = {
        "state": state.copy(),
        "mods": mods.copy(),
        "weight": weight,
        "last_msg": last_msg,
        "dominant": getattr(self, "current_dominant", None),
        "n": n,
        "max_items": max_items,
        "prev_int": prev_int,
        "prev_addr": prev_addr,
    }

    result = await asyncio.to_thread(_compute_guidelines_sync, snapshot)

    self._prev_intensity_pct = result["intensity_pct"]
    self._prev_address_score = result["address_score"]

    base_flags = result["flags"]
    extras = _gather_extras(self, state, max_items, mods)
    hostility = _compute_hostility(state, mods)
    _set_flag(extras, "HostilityLevel", f"HostilityLevel={hostility:.2f}")

    if snapshot["n"] > 5 and snapshot["last_msg"] and len(snapshot["last_msg"]) >= 12:
        extras.append("MemoryFollowUp")
        _remove_conflicts(extras)

    PRIORITY: Tuple[str, ...] = (
        "Tone", "EmotionalState", "EmotionalIntensity", "HostilityLevel", "AddressToneScore", "AddressTone",
    )
    ordered_base: List[str] = []
    for pref in PRIORITY:
        ordered_base.extend([f for f in base_flags if f.startswith(pref)])
    ordered_base += [f for f in base_flags if f not in ordered_base]
    core_slots = min(len(ordered_base), max_items - len(extras))
    final = ordered_base[:core_slots] + extras

    if human_mode:
        final = [f for f in final if f.split("=")[0] not in BOT_SIGNATURES]

    if len(final) < max_items:
        pool = [f for f in ordered_base[core_slots:] + extras if f not in final and f.split("=")[0] not in BOT_SIGNATURES]
        final += pool[: max_items - len(final)]

    seen: Set[str] = set()
    ordered: List[str] = []
    for itm in final:
        if itm not in seen:
            seen.add(itm)
            ordered.append(itm)

    device_names = {d.name for d in EXTRA_DEVICES}
    rhet = [f for f in ordered if f in device_names and f != "MemoryFollowUp"]
    mem  = [f for f in ordered if f == "MemoryFollowUp"]
    ordered = [f for f in ordered if f not in rhet + mem]

    if rhet:
        ordered.append("RhetoricalDevices=" + ",".join(rhet))
    if mem:
        ordered.append("MemoryFollowUp")

    logger.debug("Final style_guidelines (human_mode=%s): %s", human_mode, ordered)
    return ordered[:max_items]


def _compute_guidelines_sync(snapshot: dict) -> dict:
    state = snapshot["state"]
    mods = snapshot["mods"]
    weight = snapshot["weight"]
    last_msg = snapshot["last_msg"]
    n = snapshot["n"]
    max_items = snapshot["max_items"]
    prev_int = snapshot["prev_int"]
    prev_addr = snapshot["prev_addr"]
    dominant  = snapshot.get("dominant")

    gl: List[str] = []

    civ = float(mods.get("civility_mod", state.get("civility", 0.5)))
    hostility = _compute_hostility(state, mods)

    # ─── Tone ───────────────────────────
    tone_items = [
        (TONE_MAP[k], max(0.0, min(1.0, mods.get(k, state.get(k.replace("_mod",""), 0.0)))))
        for k in TONE_MAP
    ]
    tone_items.sort(key=lambda t: t[1], reverse=True)
    top3 = tone_items[:3]

    valence_mod = mods.get("valence_mod", 0.5)
    arousal_mod = mods.get("arousal_mod", 0.5)
    thr_tone = 0.75 - 0.1 * (1.0 - valence_mod) + 0.1 * (arousal_mod - 0.5)

    selected = [tone.name for tone, val in top3 if val >= thr_tone]
    if selected:
        gl[:] = [f for f in gl if not f.startswith("Tone=")]
        for name in selected:
            _append_once(gl, f"Tone+={name}")
    else:
        _set_flag(gl, "Tone", "Tone=MildlyExpressive")

    emo_state = compute_emotional_state(state, mods, dominant, allow_mixed=True)
    _set_flag(gl, "EmotionalState", f"EmotionalState={emo_state}")

    # ─── Emotional Intensity (0–100%) ─────────────
    avg_tone = sum(val for _, val in top3) / 3.0
    ar_mod = float(mods.get("arousal_mod", state.get("arousal", 0.5)))
    v_abs = abs(state.get("valence", 0.0))
    raw_norm = 0.55 * avg_tone + 0.30 * ar_mod + 0.15 * v_abs
    raw_adj = math.pow(max(0.0, min(1.0, raw_norm)), 1.1) * 100.0

    if prev_int is None:
        intensity_pct = raw_adj
    else:
        delta = raw_adj - prev_int
        alpha = 0.35 if delta > 0 else 0.20
        intensity_pct = prev_int + alpha * delta
        intensity_pct = max(0.0, min(100.0, intensity_pct))

    cap = 82.0
    if ar_mod >= 0.75 and v_abs >= 0.60:
        cap = 92.0
    intensity_pct = min(intensity_pct, cap)

    _set_flag(gl, "EmotionalIntensity", f"EmotionalIntensity={int(round(intensity_pct))}")

    # ─── Dynamic AddressTone ─────────────────────────────────
    addr_score = 0.0
    if weight is not None:
        friendliness = mods.get("friendliness_mod", 0.5)
        civility = mods.get("civility_mod", 0.5)
        base_addr = 0.6 * weight + 0.2 * friendliness + 0.2 * civility

        if prev_addr is None:
            addr_score = base_addr
        else:
            addr_score = 0.6 * prev_addr + 0.4 * base_addr

        noise = 0.05 * (1.0 - mods.get("confidence_mod", 0.5))
        addr_score = max(0.0, min(1.0, addr_score + random.uniform(-noise, noise)))
        addr_score -= 0.5 * hostility * (0.7 + 0.3 * (1.0 - civ))
        addr_score = max(0.0, min(1.0, addr_score))

        thr_warm     = 0.75 - 0.1 * (1.0 - valence_mod)
        thr_friendly = 0.50
        thr_neutral  = 0.25 + 0.1 * valence_mod

        label = None
        if addr_score >= thr_warm:
            label = "InformalWarm"
        elif addr_score >= thr_friendly:
            label = "InformalFriendly"
        elif addr_score >= thr_neutral:
            label = "InformalNeutral"
        else:
            label = "InformalIndifferent"

        if   hostility >= 0.65 and civ < 0.45: label = "DirectBlunt"
        elif hostility >= 0.45:                label = "DirectCool"

        _set_flag(gl, "AddressTone", f"AddressTone={label}")

    _set_flag(gl, "AddressToneScore", f"AddressToneScore={addr_score:.2f}")

    return {
        "flags":         [getattr(f, "name", f) for f in gl],
        "intensity_pct": intensity_pct,
        "address_score": addr_score,
    }
EOF