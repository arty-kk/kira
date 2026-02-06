#app/emo_engine/persona/stylers/guidelines.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import secrets
import time

from typing import Dict, List, Set, Tuple, Optional

from app.config import settings
from ..constants.extra_devices import EXTRA_DEVICES, BOT_SIGNATURES
from ..constants.tone_map import TONE_MAP
from ..constants.emotional_state import compute_emotional_state
from ..memory import get_embedding
from ..executor import EXECUTOR

logger = logging.getLogger(__name__)


def _metric(state: Dict[str, float], mods: Dict[str, float], key: str, fallback: float = 0.5) -> float:
    if not key:
        return float(fallback)
    raw = mods.get(key)
    if raw is None:
        raw = state.get(key.replace("_mod", ""), fallback)
    try:
        return float(raw)
    except Exception:
        return float(fallback)


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

    if "valence_mod" in mods:
        V_signed = 2.0 * float(mods.get("valence_mod", 0.5)) - 1.0
    else:
        V_signed = float(state.get("valence", 0.0))
    neg_val  = max(0.0, -V_signed)
    arousal  = float(mods.get("arousal_mod",   state.get("arousal",   0.5)))
    dom      = float(mods.get("dominance_mod", state.get("dominance", 0.33)))
    fatigue  = float(mods.get("fatigue_mod",   state.get("fatigue",   0.0)))
    anger    = float(mods.get("anger_mod",     state.get("anger",     0.0)))
    contempt = float(mods.get("contempt_mod",  state.get("contempt",  0.0)))
    disgust  = float(mods.get("disgust_mod",   state.get("disgust",   0.0)))
    rage     = float(mods.get("rage_mod",      state.get("rage",      0.0)))
    neg_affect = max(
        neg_val,
        0.6 * anger + 0.4 * contempt + 0.3 * disgust + 0.5 * rage
    )
    h = neg_affect * (0.5 + 0.5 * arousal) * (0.5 + 0.5 * dom) * (1.0 - 0.3 * fatigue)
    return max(0.0, min(1.0, h))

def _ei_code(pct: float) -> str:
    p = max(0.0, min(100.0, float(pct)))
    if p <= 25:
        return "VeryLow"
    if p <= 40:
        return "Low"
    if p <= 54:
        return "Normal"
    if p <= 67:
        return "Moderate"
    if p <= 79:
        return "High"
    if p <= 90:
        return "VeryHigh"
    return "Extreme"

def _hostility_code(h: float) -> str:
    x = max(0.0, min(1.0, float(h)))
    if x < 0.15:
        return "FriendlyAttitude"
    if x < 0.33:
        return "PositiveAttitude"
    if x < 0.66:
        return "NeutralAttitude"
    if x < 0.85:
        return "NegativeAttitude"
    return "HostileAttitude"

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
        key = getattr(device, "metric_key", None)
        value = _metric(state, mods, key) if key else 0.5
        try:
            ok = device.should_apply(value, rng)
        except Exception:
            ok = False
        if ok:
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
        "chat_id": getattr(self, "chat_id", None),
        "state_version": getattr(self, "state_version", None),
        "uid": uid,
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

    snapshot["mode_id"] = getattr(self, "current_mode_id", None)
    snapshot["mode_stats"] = getattr(self, "current_mode_stats", None) or {}

    snapshot["tone_hist"] = getattr(self, "_tone_hist", [])

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(EXECUTOR, _compute_guidelines_sync, snapshot)

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

    mode_stats = getattr(self, "current_mode_stats", None) or {}
    try:
        novelty = float(mode_stats.get("novelty", 0.0))
    except Exception:
        novelty = 0.0
    try:
        coherence = float(mode_stats.get("coherence", 0.5))
    except Exception:
        coherence = 0.5

    creativity_drive = max(0.0, min(1.0, 0.6 * novelty + 0.4 * (1.0 - coherence)))
    extra_scale = 0.7 + 0.7 * creativity_drive   # 0.7 .. 1.4
    eff_max_for_extras = max(2, int(round(max_items * extra_scale)))

    extras = _gather_extras(self, state, eff_max_for_extras, mods)
    hostility = _compute_hostility(state, mods)
    _set_flag(base_flags, "HostilityLevel", f"HostilityLevel={_hostility_code(hostility)}")

    def _low_info(s: str) -> bool:
        if not s:
            return True
        s2 = s.strip()
        if len(s2) < 16:
            return True
        return bool(re.fullmatch(r"[\W_]+", s2))

    allow_mem_fu = (n > 0) and bool(last_msg) and (not _low_info(last_msg))
    trust_ema = None
    if uid is not None:
        try:
            trust_ema = float(getattr(self, "attachments", {}).get(uid, {}).get("trust_ema", 0.5))
        except Exception:
            trust_ema = None
    min_trust = float(getattr(settings, "MEMORYFOLLOWUP_MIN_TRUST", 0.3))
    if trust_ema is not None and trust_ema < min_trust:
        allow_mem_fu = False
    if allow_mem_fu:
        try:
            extras.append("MemoryFollowUp")
            _remove_conflicts(extras)
        except Exception:
            pass

    try:
        if last_msg and (not _low_info(last_msg)):
            emb = None
            cached_emb = getattr(self, "_last_msg_emb", None)
            cached_text = getattr(self, "_last_msg_emb_text", None)
            if cached_emb is not None and last_msg == cached_text:
                emb = cached_emb
            if emb is None:
                emb_task = getattr(self, "_emb_inflight", None)
                emb_task_text = getattr(self, "_emb_inflight_text", None)
                if emb_task is not None and emb_task_text == last_msg:
                    if emb_task.done():
                        try:
                            if not emb_task.cancelled() and emb_task.exception() is None:
                                emb = emb_task.result()
                        except Exception:
                            emb = None
                    if emb is None:
                        try:
                            emb = await asyncio.wait_for(asyncio.shield(emb_task), timeout=0.4)
                        except Exception:
                            emb = None
                    if emb:
                        self._last_msg_emb = emb
                        self._last_msg_emb_text = last_msg
            if emb is None:
                _t1 = time.perf_counter()
                try:
                    emb = await asyncio.wait_for(get_embedding(last_msg), timeout=4.0)
                    logger.info("openai_request name=get_embedding duration_ms=%.1f", (time.perf_counter() - _t1) * 1000.0)
                except asyncio.TimeoutError:
                    logger.warning("openai_timeout name=get_embedding after_ms=%.1f", (time.perf_counter() - _t1) * 1000.0)
                    raise
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
                    thr = float(getattr(settings, "MIN_MEMORY_SIMILARITY", 0.28))
                    if _sim >= thr:
                        extras.append("RecallPastSnippet")
    except Exception:
        pass

    PRIORITY: Tuple[str, ...] = (
        "EmotionalState", "EmotionalIntensity", "Tone", "AddressTone", "AddressToneScore",
        "HostilityLevel", "MemoryFollowUp", "RhetoricalDevices", "EmojiTouch", "VarySentenceLength",
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
        if itm in seen:
            continue
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

    PRIORITY_ORDER = (
        "EmotionalState", "EmotionalIntensity", "Tone", "AddressTone", "AddressToneScore",
        "HostilityLevel", "MemoryFollowUp", "RhetoricalDevices", "EmojiTouch", "VarySentenceLength",
    )
    ordered = sorted(
        ordered,
        key=lambda f: next((i for i, p in enumerate(PRIORITY_ORDER) if f.startswith(p)), 999)
    )

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
    mode_id   = snapshot.get("mode_id")
    mode_stats = snapshot.get("mode_stats") or {}

    gl: List[str] = []

    def _as_f(stats: dict, key: str, default: float) -> float:
        try:
            return float(stats.get(key, default))
        except Exception:
            return default

    mode_nov = _as_f(mode_stats, "novelty", 0.0)
    mode_coh = _as_f(mode_stats, "coherence", 0.5)
    mode_int = _as_f(mode_stats, "intensity", 0.5)
    mode_cplx = _as_f(mode_stats, "complexity", 0.0)

    mode_exp_drive = max(0.0, min(1.0, 0.6 * mode_nov + 0.3 * mode_int + 0.1 * mode_cplx))
    mode_stab_drive = max(
        0.0,
        min(1.0, 0.6 * mode_coh + 0.2 * (1.0 - mode_nov) + 0.2 * (1.0 - mode_int)),
    )

    try:
        mode_index = None
        if isinstance(mode_id, str) and mode_id.startswith("mode_"):
            mode_index = int(mode_id.split("_", 1)[1])
    except Exception:
        mode_index = None

    civ = float(mods.get("civility_mod", state.get("civility", 0.5)))
    hostility = _compute_hostility(state, mods)

    # ─── Tone ───────────────────────────
    tone_items = [
        (TONE_MAP[k], max(0.0, min(1.0, mods.get(k, state.get(k.replace("_mod",""), 0.0)))))
        for k in TONE_MAP
        if k != "valence_mod"
    ]
    tone_items.sort(key=lambda t: t[1], reverse=True)
    topk = tone_items[:3] if len(tone_items) >= 3 else tone_items

    if "valence_mod" in mods:
        v_signed = 2.0 * float(mods.get("valence_mod", 0.5)) - 1.0
    else:
        v_signed = float(state.get("valence", 0.0))
    arousal_mod = float(mods.get("arousal_mod", state.get("arousal", 0.5)))
    fatigue     = float(mods.get("fatigue_mod", state.get("fatigue", 0.0)))
    surprise    = float(mods.get("surprise_mod", state.get("surprise", 0.0)))

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
        exps = [math.exp(logit_value) for _, logit_value in logits]
        Z = sum(exps)
        probs = [e / Z for e in exps] if Z > 0 else [1.0 / len(exps)] * len(exps)
    else:
        exps = []
        probs = []

    state_seed = int(snapshot["state"].get("seed", 0)) & 0xFFFFFFFF
    if n is None:
        base_seed = secrets.randbits(32)
    else:
        payload = [
            snapshot.get("chat_id"),
            snapshot.get("state_version"),
            snapshot.get("uid"),
            prev_int,
            prev_addr,
        ]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        base_seed = int.from_bytes(hashlib.sha256(serialized.encode("utf-8")).digest()[:4], "big") & 0xFFFFFFFF
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
        if name == "Valence":
            continue
        _append_once(gl, f"Tone+={name}")

    emo_state = compute_emotional_state(state, mods, dominant, allow_mixed=True)
    _set_flag(gl, "EmotionalState", f"EmotionalState={emo_state}")

    # ─── Emotional Intensity (human-like) ─────────────
    avg_tone = (sum(val for _, val in topk) / max(1, len(topk)))
    ar_mod   = arousal_mod
    v_abs    = abs(v_signed)
    surprise = float(mods.get("surprise_mod", state.get("surprise", 0.0)))
    fatigue  = float(mods.get("fatigue_mod",  state.get("fatigue",  0.0)))
    salience = base_weight if base_weight is not None else 0.0

    calm_pack = (
        float(mods.get("calm_mod",         state.get("calm",         0.0))) +
        float(mods.get("tranquility_mod",  state.get("tranquility",  0.0))) +
        float(mods.get("peace_mod",        state.get("peace",        0.0))) +
        float(mods.get("comfort_mod",      state.get("comfort",      0.0)))
    ) / 4.0
    raw_norm = 0.52 * avg_tone + 0.30 * ar_mod + 0.18 * v_abs
    raw_norm -= 0.35 * calm_pack * (1.0 - ar_mod)
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

    mode_factor = 1.0 + 0.18 * (mode_exp_drive - mode_stab_drive)
    if mode_factor < 0.80:
        mode_factor = 0.80
    elif mode_factor > 1.25:
        mode_factor = 1.25

    intensity_pct *= mode_factor

    if mode_index is None or mode_index == 0:
        mid = 55.0
        dev = intensity_pct - mid
        intensity_pct = mid + 0.7 * dev
    elif mode_index is not None and mode_index >= 6:
        if intensity_pct < 40.0:
            intensity_pct = 40.0 + 0.6 * (intensity_pct - 40.0)

    intensity_pct = max(0.0, min(100.0, intensity_pct))
    _set_flag(gl, "EmotionalIntensity", f"EmotionalIntensity={_ei_code(intensity_pct)}")

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

        if mode_index is None or mode_index == 0:
            mid = 0.5
            dev = addr_score - mid
            addr_score = max(0.0, min(1.0, mid + 0.7 * dev))
        elif mode_index is not None and mode_index >= 6:
            addr_score = max(0.0, min(1.0, addr_score + 0.10 * mode_exp_drive))

        v_pos = (v_signed + 1.0) * 0.5
        thr_warm     = 0.65 + 0.20 * (1.0 - v_pos)
        thr_friendly = 0.50
        thr_neutral  = 0.25 + 0.15 * v_pos

        label = None
        if addr_score >= thr_warm:
            label = "InformalWarm"
        elif addr_score >= thr_friendly:
            label = "InformalFriendly"
        elif addr_score >= thr_neutral:
            label = "InformalNeutral"
        else:
            label = "InformalIndifferent"

        _label = label
    else:
        _label = "InformalNeutral"

    if hostility >= 0.65 and civ < 0.45:
        _label = "DirectBlunt"
    elif hostility >= 0.45:
        _label = "DirectCool"
    _set_flag(gl, "AddressTone", f"AddressTone={_label}")

    _set_flag(gl, "AddressToneScore", f"AddressToneScore={addr_score:.2f}")

    return {
        "flags":         [getattr(f, "name", f) for f in gl],
        "intensity_pct": intensity_pct,
        "address_score": addr_score,
        "chosen_tone":   (chosen.name if 'chosen' in locals() and chosen else None),
    }
