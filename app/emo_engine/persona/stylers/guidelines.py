cat >app/emo_engine/persona/stylers/guidelines.py<< 'EOF'
#app/emo_engine/persona/stylers/guidelines.py
from __future__ import annotations

import asyncio
import logging
import random
import math
import re
import time
import secrets

from typing import Dict, List, Set, Tuple, Optional

from app.config import settings
from ..constants.extra_devices import EXTRA_DEVICES, BOT_SIGNATURES
from ..constants.tone_map import TONE_MAP
from ..constants.emotional_state import compute_emotional_state
from ..memory import get_embedding

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
            if len(chosen) >= max(2, max_items // 3):
                break
    return chosen


async def style_guidelines(
    self,
    uid: Optional[int] = None,
    max_items: int = 18,
    *,
    human_mode: bool = True,
) -> List[str]:

    state = self.state.copy()
    try:
        _cid = int(self.chat_id)
    except Exception:
        _cid = hash(str(getattr(self, "chat_id", ""))) & 0xFFFFFFFF
    try:
        _ver = int(self.state_version)
    except Exception:
        _ver = hash(str(getattr(self, "state_version", 0))) & 0xFFFFFFFF
    state["seed"] = ((_cid << 32) ^ _ver) & 0xFFFFFFFFFFFFFFFF
    mods = self._mods_cache.copy()
    if (not mods) or (getattr(self, "_style_mods_version", -1) != self.state_version):
        try:
            mods = await asyncio.wait_for(self.style_modifiers(), timeout=5.0)
            try:
                self._mods_cache = dict(mods)
                self._style_mods_version = self.state_version
            except Exception:
                logger.debug("failed to update mods cache", exc_info=True)
        except asyncio.TimeoutError:
            logger.warning(
                "style_modifiers() timed-out; using previous cache (%d items)",
                len(self._mods_cache),
            )

    try:
        weight = self._decayed_weight(uid) if uid is not None else None
    except Exception:
        weight = None
    att_v = None
    if uid is not None and hasattr(self, "attachments"):
        try:
            rec = self.attachments.get(uid)
            if rec is not None:
                att_v = float(rec.get("value", None))
        except Exception:
            att_v = None
    last_msg = getattr(self, "_last_user_msg", "") or ""

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
        "attachment": att_v,
        "last_msg": last_msg,
        "dominant": getattr(self, "current_dominant", None),
        "n": n,
        "max_items": max_items,
        "prev_int": prev_int,
        "prev_addr": prev_addr,
    }

    snapshot["tone_hist"] = getattr(self, "_tone_hist", [])

    result = await asyncio.to_thread(_compute_guidelines_sync, snapshot)

    chosen_tone = result.get("chosen_tone")
    if chosen_tone:
        hist = list(getattr(self, "_tone_hist", []))
        hist.append((chosen_tone, time.time()))
        if len(hist) > 6:
            hist = hist[-6:]
        self._tone_hist = hist

    self._prev_intensity_pct = result["intensity_pct"]
    self._prev_address_score = result["address_score"]

    base_flags = result["flags"]
    extras = _gather_extras(self, state, max_items, mods)
    hostility = _compute_hostility(state, mods)
    _set_flag(base_flags, "HostilityLevel", f"HostilityLevel={hostility:.2f}")

    # Offer a follow-up question only when assistance is actually allowed and there is prior context
    def _low_info(s: str) -> bool:
        if not s:
            return True
        s2 = s.strip()
        if len(s2) < 16:
            return True
        return bool(re.fullmatch(r"[\W_]+", s2))

    allow_mem_fu = (n > 0) and bool(last_msg) and (not _low_info(last_msg))
    if allow_mem_fu:
        try:
            extras.append("MemoryFollowUp")
            _remove_conflicts(extras)
        except Exception:
            pass

    try:
        if last_msg and (not _low_info(last_msg)):
            emb = await asyncio.wait_for(get_embedding(last_msg), timeout=4.0)
            if emb and any(emb):
                try:
                    hits = await asyncio.wait_for(
                        self.enhanced_memory.query(emb, top_k=1, uid=uid),
                        timeout=getattr(settings, "REDISSEARCH_TIMEOUT", 3)
                    )
                except Exception:
                    hits = []
                if hits:
                    _txt, _sim = hits[0]
                    thr = float(getattr(settings, "MIN_MEMORY_SIMILARITY", 0.62))
                    if _sim >= thr:
                        extras.append("RecallPastSnippet")
    except Exception:
        pass

    PRIORITY: Tuple[str, ...] = (
        "EmotionalState", "EmotionalIntensity", "Tone", "AddressTone", "AddressToneScore",
        "HostilityLevel", "RhetoricalDevices", "EmojiTouch", "VarySentenceLength",
    )
    ordered_base: List[str] = []
    for pref in PRIORITY:
        ordered_base.extend([f for f in base_flags if f.startswith(pref)])
    ordered_base += [f for f in base_flags if f not in ordered_base]
    core_slots = max(0, min(len(ordered_base), max_items - len(extras)))
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
    attachment = snapshot.get("attachment")
    base_weight = attachment if attachment is not None else weight
    last_msg = snapshot["last_msg"]
    n = snapshot["n"]
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
    topk = tone_items[:3] if len(tone_items) >= 3 else tone_items

    valence_mod = float(mods.get("valence_mod", 0.0))
    arousal_mod = float(mods.get("arousal_mod", 0.5))
    fatigue     = float(mods.get("fatigue_mod", state.get("fatigue", 0.0)))
    surprise    = float(state.get("surprise", 0.0))

    T = 0.80 + 0.25*(arousal_mod - 0.5) + 0.15*surprise - 0.20*fatigue
    T = max(0.50, min(1.10, T))

    tone_hist = snapshot.get("tone_hist", [])
    hist_names = [h[0] if isinstance(h, (tuple, list)) else str(h) for h in tone_hist]
    last_name = hist_names[-1] if hist_names else None

    rep = 0
    for name in reversed(hist_names):
        if name == last_name:
            rep += 1
        else:
            break
    repeat_penalty = 0.18 * min(3, max(0, rep - 1))

    if topk:
        m = max(v for _, v in topk)
    else:
        m = 0.0
    logits = []
    for tone, v in topk:
        logit = (v - m) / max(1e-6, T)
        if tone.name == last_name and rep >= 2:
            logit -= repeat_penalty
        logits.append((tone, logit))

    if logits:
        exps = [math.exp(l) for _, l in logits]
        Z = sum(exps)
        probs = [e / Z for e in exps] if Z > 0 else [1.0 / len(exps)] * len(exps)
    else:
        exps = []
        probs = []

    state_seed = int(snapshot["state"].get("seed", 0)) & 0xFFFFFFFF
    base_seed = (hash((n, prev_int, prev_addr)) & 0xFFFFFFFF) if (n is not None) else secrets.randbits(32)
    seed = (base_seed ^ state_seed) & 0xFFFFFFFF
    rng = random.Random(seed)
    r = rng.random()
    cum = 0.0
    chosen = None
    for (tone, _), p in zip(logits, probs):
        cum += p
        if r <= cum:
            chosen = tone
            break
    if chosen is None and topk:
        chosen = topk[0][0]

    band = max(0.04, 0.06 + 0.06*(arousal_mod - 0.5) - 0.04*fatigue)
    selected = []
    if chosen:
        selected.append(chosen.name)
        chosen_val = next((v for t, v in topk if t.name == chosen.name), None)
        if chosen_val is not None:
            for tone, v in topk:
                if tone.name == chosen.name:
                    continue
                if (chosen_val - v) <= band:
                    selected.append(tone.name)

    gl[:] = [f for f in gl if not (f.startswith("Tone=") or f.startswith("Tone+="))]
    if not selected and topk:
        selected = [topk[0][0].name]

    for name in selected:
        _append_once(gl, f"Tone+={name}")

    emo_state = compute_emotional_state(state, mods, dominant, allow_mixed=True)
    _set_flag(gl, "EmotionalState", f"EmotionalState={emo_state}")

    # ─── Emotional Intensity (human-like) ─────────────
    avg_tone = (sum(val for _, val in topk) / max(1, len(topk)))
    ar_mod   = float(mods.get("arousal_mod", state.get("arousal", 0.5)))
    v_abs    = abs(state.get("valence", 0.0))
    surprise = float(state.get("surprise", 0.0))
    fatigue  = float(mods.get("fatigue_mod", state.get("fatigue", 0.0)))
    salience = base_weight if base_weight is not None else 0.0

    raw_norm = 0.52 * avg_tone + 0.30 * ar_mod + 0.18 * v_abs
    raw_adj  = (max(0.0, min(1.0, raw_norm)) ** 1.12) * 100.0

    if prev_int is None:
        prev_int = 50.0

    evidence = (
        0.36 * ar_mod +
        0.28 * v_abs +
        0.18 * salience +
        0.12 * surprise +
        0.06 * hostility
    )
    evidence = max(0.0, min(1.0, evidence))

    if rep >= 2:
        evidence *= (1.0 - 0.08 * min(3, rep - 1))

    thr_up = 0.60 + 0.35 * ((prev_int / 100.0) ** 1.6)

    def _gate_cap(e: float) -> float:
        s = 1.0 / (1.0 + math.exp(-(e - 0.80) / 0.08))
        return 55.0 + 45.0 * s

    target_cap = _gate_cap(evidence)
    target_cap -= 6.0 * fatigue
    target_cap = max(60.0, min(96.0, target_cap))

    target = min(raw_adj, target_cap)

    delta = target - prev_int

    strong_push  = max(0.0, evidence - thr_up)
    alpha_up     = 0.12 + 0.28 * strong_push
    alpha_down   = 0.38

    if prev_int >= 70.0 and delta > 0:
        alpha_up *= 0.35

    max_step_up   = 3.5 if prev_int < 70.0 else 2.0
    max_step_down = 12.0

    if delta > 0:
        delta = min(delta, max_step_up)
        intensity_pct = prev_int + alpha_up * delta
    else:
        delta = max(delta, -max_step_down)
        intensity_pct = prev_int + alpha_down * delta

    knee = 82.0 - 4.0 * fatigue
    knee = max(76.0, min(86.0, knee))
    if intensity_pct > knee:
        over = intensity_pct - knee
        denom = max(1e-6, (100.0 - knee))
        intensity_pct = knee + (100.0 - knee) * (1.0 - math.exp(-over / denom))

    intensity_pct = max(0.0, min(100.0, intensity_pct))
    _set_flag(gl, "EmotionalIntensity", f"EmotionalIntensity={int(round(intensity_pct))}")

    # ─── Dynamic AddressTone ─────────────────────────────────
    addr_score = 0.0
    if base_weight is not None:
        friendliness = mods.get("friendliness_mod", 0.5)
        civility = mods.get("civility_mod", 0.5)
        base_addr = 0.6 * base_weight + 0.2 * friendliness + 0.2 * civility + 0.08
        base_addr = max(0.0, min(1.0, base_addr))

        if prev_addr is None:
            addr_score = base_addr
        else:
            addr_score = 0.6 * prev_addr + 0.4 * base_addr

        noise_amp = 0.05 * (1.0 - mods.get("confidence_mod", 0.5))
        noise_seed = (seed ^ 0x9E3779B9) & 0xFFFFFFFF
        rng_addr = random.Random(noise_seed)
        addr_score = max(0.0, min(1.0, addr_score + rng_addr.uniform(-noise_amp, noise_amp)))
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
    else:
        _set_flag(gl, "AddressTone", "AddressTone=InformalNeutral")

    _set_flag(gl, "AddressToneScore", f"AddressToneScore={addr_score:.2f}")

    return {
        "flags":         [getattr(f, "name", f) for f in gl],
        "intensity_pct": intensity_pct,
        "address_score": addr_score,
        "chosen_tone":   (chosen.name if 'chosen' in locals() and chosen else None),
    }
EOF