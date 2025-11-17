#app/emo_engine/persona/core.py
from __future__ import annotations

import asyncio
import json
import random
import hashlib
import time
import inspect
import logging
import re
import unicodedata
import weakref

from collections import defaultdict, deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, ClassVar, Optional, Any, TypeVar
from collections.abc import Coroutine, Callable
from types import MethodType

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.core.memory import record_activity as _mem_record_activity

from .memory import PersonaMemory, get_embedding
from .ltm import LongTermMemory
from .states import (
    _recompute_rates, process_interaction as _process_interaction_impl,
    _blend_metric, _update_mood_label, _bg_worker,
    _decayed_weight, _compute_salience, _update_weight,
    _update_attachment, _ensure_attachment_defaults,
    _effective_person_weight, _apply_attachment_influence,
)
from .utils.emotion_math import (
    _compute_secondary, _clamp as _global_clamp,
    _compute_tertiary,
)
from .utils.text_analyzer import TextAnalyzer
from .stylers.modifiers import style_modifiers
from .stylers.guidelines import style_guidelines
from .constants.tone_map import Tone
from .constants.labels import EMO_LABEL_MAP as EMO_LABELS
from .constants.metrics_keys import metrics_keys as all_key_mods
from .constants.emotions import (  
    ALL_METRICS, PRIMARY_EMOTIONS, SECONDARY_EMOTIONS,
    TERTIARY_EMOTIONS, DYAD_KEYS, TRIAD_KEYS,
    COGNITIVE_METRICS, EXTRA_TRIGGER_METRICS,
)
from .constants.user_prefs import (
    ZODIAC_SET, SOCIALITY_SET, ARCHETYPES_SET, TEMP_KEYS, MAX_ARCH
)

logger = logging.getLogger(__name__)

def _normalize_temperament(t: dict | None, default_json: str) -> dict:
    try:
        default_map = json.loads(default_json)
    except Exception:
        default_map = {"sanguine": 0.4, "choleric": 0.25, "phlegmatic": 0.20, "melancholic": 0.15}

    try:
        base = {k: float(t.get(k, 0.0)) for k in TEMP_KEYS} if isinstance(t, dict) else {}
    except Exception:
        base = {}
    if not base or not any(base.values()):
        return default_map

    base = {k: max(0.0, min(1.0, v)) for k, v in base.items()}
    s = sum(base.values())
    if s <= 0.0:
        return default_map
    base = {k: (v / s) for k, v in base.items()}
    return base

@dataclass
class Persona:
    chat_id: int
    name: str = settings.PERSONA_NAME
    age: int = settings.PERSONA_AGE
    gender: str = settings.PERSONA_GENDER
    zodiac: str = settings.PERSONA_ZODIAC
    temperament: Dict[str, float] = field(
        default_factory=lambda: json.loads(settings.PERSONA_TEMPERAMENT)
    )
    sociality: str = field(default="extrovert")
    archetypes: list[str] = field(
        default_factory=lambda: json.loads(
            getattr(settings, "PERSONA_ARCHETYPES", '["Rebel","Jester","Sage"]')
        )
    )
    role: str = settings.PERSONA_ROLE
    state: Dict[str, float] = field(init=False, default_factory=dict)
    change_rates: Dict[str, float] = field(init=False, default_factory=dict)
    user_gender: str = field(init=False, default="unknown", repr=False)
    mood: str = "steady"
    _dirty_metrics: set[str] = field(init=False, default_factory=set)
    enhanced_memory: PersonaMemory = field(init=False, repr=False, compare=False)
    user_weights: Dict[int, List[float]] = field(init=False, default_factory=dict)
    last_user_emotions: list = field(init=False, default_factory=list)
    _just_pushed_back: bool = field(init=False, default=False)
    _restored_evt: asyncio.Event = field(init=False, repr=False, compare=False, default=None)
    state_version: int = field(init=False, default=0)
    ema: dict = field(init=False, default_factory=dict)
    dominant_threshold: float = field(init=False, default=0.0)
    dominant_locked: bool = field(init=False, default=False)
    current_dominant: str = field(init=False, default=None)
    _rng: random.Random = field(init=False, repr=False, compare=False, default=None)
    _last_prompt_version: int = field(init=False, default=-1)
    _last_prompt_guidelines: str = field(init=False, default=None)
    _prompt_cache: str = field(init=False, default="")
    _loop_id: int = field(init=False, default=0)
    _cached_style_modifiers: dict = field(init=False, default_factory=dict)
    _mods_cache: dict = field(init=False, default_factory=dict)
    _last_uid: int = field(init=False, default=None)
    _memfu_local_cache: dict = field(init=False, default_factory=dict)
    flowstate: float = field(init=False, default=0.0)
    _style_mods_version: int = field(init=False, default=-1)
    _last_user_msg: str = field(init=False, default="")
    attachments: Dict[int, dict] = field(init=False, default_factory=dict)
    _rds_loop_id: Optional[int] = field(init=False, repr=False, compare=False, default=None)
    _user_locks: Dict[int, asyncio.Lock] = field(init=False, default_factory=dict, repr=False, compare=False)
    _bg_started: bool = field(init=False, default=False)
    _bg_task: Optional[asyncio.Task] = field(init=False, default=None)
    _bg_start_lock: asyncio.Lock = field(init=False, repr=False, compare=False)
    _worker_task: Optional[asyncio.Task] = field(init=False, default=None, repr=False, compare=False)
    _spawned_tasks: set = field(init=False, default_factory=set, repr=False, compare=False)
    _memfu_cap: int = field(init=False, default=256)
    _memfu_ttl: float = field(init=False, default=3600.0)
    _recompute_rates = _recompute_rates
    _blend_metric = _blend_metric
    _update_mood_label = _update_mood_label
    _compute_secondary = _compute_secondary
    _compute_tertiary = _compute_tertiary
    _decayed_weight = _decayed_weight
    style_guidelines = style_guidelines
    style_modifiers = style_modifiers
    _compute_salience = _compute_salience
    _update_weight = _update_weight
    _clamp = staticmethod(_global_clamp)
    
    _EMO_LABEL_MAP: ClassVar[dict[str, str]] = EMO_LABELS

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()
        self._bg_queue = asyncio.Queue(maxsize=getattr(settings, "BG_QUEUE_MAX", 1000))
        self._mods_lock = asyncio.Lock()
        self._proc_sem = asyncio.Semaphore(1)
        self._bg_start_lock = asyncio.Lock()
        self._rng = random.Random(self.chat_id)
        self._restored_evt = asyncio.Event()
        self._in_peak = False
        self._last_mono = time.monotonic()
        self._last_valence_peak_ts = time.time()
        self._style_mods_version = -1
        for fn in (
            _recompute_rates, _blend_metric, _update_mood_label,
            _compute_secondary, _compute_tertiary, _bg_worker,
            _decayed_weight, _compute_salience, _update_weight,
            _update_attachment, _ensure_attachment_defaults,
            _effective_person_weight, _apply_attachment_influence,
        ):
            setattr(self, fn.__name__, MethodType(fn, self))

        self.style_modifiers = MethodType(style_modifiers, self)
        self.style_guidelines = MethodType(style_guidelines, self)
        self._recompute_rates()
        
        try:
            self._loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            self._loop_id = None

        center = settings.EMO_INITIAL_CENTER
        self.state = {m: center for m in ALL_METRICS}
        try:
            self.state["valence"] = float(getattr(settings, "VALENCE_BASELINE", 0.15))
        except Exception:
            self.state["valence"] = 0.15
        for subs in SECONDARY_EMOTIONS.values():
            for name in subs.keys():
                self.state.setdefault(name, 0.0)
        for subs in TERTIARY_EMOTIONS.values():
            for name in subs.keys():
                self.state.setdefault(name, 0.0)
        for name in DYAD_KEYS:
            self.state.setdefault(name, 0.0)
        for name in TRIAD_KEYS:
            self.state.setdefault(name, 0.0)
        for name in EXTRA_TRIGGER_METRICS:
            self.state.setdefault(name, 0.0)
        self.state.setdefault("dominance", 0.5)
        self.state_version = 0
        extra_ema = ["confidence", "humor", "charisma", "authority", "wit"]
        self.ema = {e: 0.5 for e in PRIMARY_EMOTIONS + ["arousal", "energy", "fatigue"] + extra_ema}
        self.ema["valence"] = 0.0
        self.dominant_threshold = settings.EMO_THRESHOLD_DOMINANT
        self.dominant_locked = False
        self.current_dominant = None
        self._last_prompt_version = -1
        self._last_prompt_guidelines = None
        self._prompt_cache = ""
        self.last_user_emotions = []
        self._just_pushed_back = False
        self._cached_style_modifiers = {}
        self._mods_cache = {}
        self._last_uid = None
        self.flowstate = 0.0
        self.enhanced_memory = PersonaMemory(chat_id=self.chat_id)
        try:
            self.enhanced_memory.parent = weakref.proxy(self)
        except Exception:
            self.enhanced_memory.parent = self
        self.ltm = LongTermMemory()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Persona init: no running event loop detected; background tasks will start lazily.")
        self._last_mood_change_ts = time.time()
        self._prev_mood = self.mood
        self._text_analyzer = TextAnalyzer()
        self.attachments = {}
        self._rds = None
        try:
            cap = int(getattr(settings, "MEM_RECENT_SKETCH_CAP", 100))
        except Exception:
            cap = 100
        self._recent_sketch = deque(maxlen=max(10, cap))
        try:
            self._memfu_cap = int(getattr(settings, "MEMFU_LOCAL_CACHE_MAX", 256))
        except Exception:
            self._memfu_cap = 256
        try:
            self._memfu_ttl = float(getattr(settings, "MEMFU_LOCAL_CACHE_TTL_SECS", 3600.0))
        except Exception:
            self._memfu_ttl = 3600.0

    async def ready(self, timeout: float | None = 5.0) -> bool:
        try:
            await self._ensure_background_started()
            if self._restored_evt.is_set():
                return True
            if timeout is None or timeout <= 0:
                return False
            try:
                await asyncio.wait_for(self._restored_evt.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        except Exception:
            logger.debug("Persona.ready() failed", exc_info=True)
            return False

    def _user_lock(self, uid: int) -> asyncio.Lock:
        lock = self._user_locks.get(uid)
        if lock is None:
            try:
                max_locks = int(getattr(settings, "USER_LOCK_MAX", 5000))
                if len(self._user_locks) > max_locks:
                    to_drop = int(max(1, max_locks * 0.05))
                    dropped = 0
                    for k in list(self._user_locks.keys()):
                        if dropped >= to_drop:
                            break
                        lk = self._user_locks.get(k)
                        if lk and not lk.locked():
                            self._user_locks.pop(k, None)
                            dropped += 1
            except Exception:
                pass
            lock = asyncio.Lock()
            self._user_locks[uid] = lock
        return lock

    async def analyze_text(self, text: str) -> Dict[str, float]:
        return await self._text_analyzer.analyze_text(text)

    async def _notify_ready(self) -> None:
        await self.enhanced_memory.ready()
        self._restored_evt.set()

    async def _start_bg_worker(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return

        self._worker_task = asyncio.create_task(self._bg_worker(), name="persona-bg-worker")
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("persona BG worker crashed")

    async def _ensure_background_started(self) -> None:
        try:
            _ = asyncio.get_running_loop()
        except RuntimeError:
            return
        async with self._bg_start_lock:
            if self._bg_started and self._worker_task and not self._worker_task.done():
                return
            self._bg_task = self._spawn(self._start_bg_worker, name="persona-start-worker")
            self._spawn(self._notify_ready, name="persona-notify-ready")
            self._bg_started = True

    def tweak(self, knob: str, delta: float) -> None:
        if knob not in COGNITIVE_METRICS:
            raise ValueError(f"Unknown knob: {knob}")
        cur = self.state.get(knob, 0.5)
        self.state[knob] = self._clamp(
            cur + delta,
            0.0,
            1.0,
        )

    async def to_prompt(self, guidelines: List[str]) -> str:

        await self._ensure_background_started()
        query_emb = getattr(self, "_last_msg_emb", None)
        
        def _norm_key(s: str) -> str:
            if not isinstance(s, str):
                s = str(s)
            s = unicodedata.normalize("NFKC", s)
            s = s.strip().strip('"\'')

            return re.sub(r"\s+", " ", s)

        start_ts = time.time()
        guide_names = ",".join(g.name if hasattr(g, "name") else str(g) for g in guidelines)
        logger.debug("▶ to_prompt START chat=%s version=%s guides=%s",
                    self.chat_id, self.state_version, guide_names)

        norm_guides = [g.name if hasattr(g, "name") else str(g) for g in guidelines]
        guide_str_key = ",".join(sorted(set(norm_guides)))
        unique_guides = list(dict.fromkeys(norm_guides))
        msg_hash = hashlib.md5((self._last_user_msg or "").encode("utf-8")).hexdigest()[:8]
        cache_key = f"{self.state_version}|{self._last_uid or 0}|{guide_str_key}|{msg_hash}"
        if cache_key == self._last_prompt_guidelines:
            return self._prompt_cache
        norm_guides_set = set(norm_guides)
        want_mem_followup = "MemoryFollowUp" in norm_guides_set
        want_recall_snippet = "RecallPastSnippet" in norm_guides_set
        want_any_memory = (want_mem_followup or want_recall_snippet)
        try:
            use_llm_selector = bool(getattr(settings, "MEMORYFOLLOWUP_USE_LLM_SELECTOR", True))
        except Exception: use_llm_selector = True
        has_query = bool(self._last_user_msg)

        #metrics_str = "; ".join(f"{k}={s.get(k, 0.0):.2f}" for k in all_key_mods)

        #logger.info("   ↳ style_modifiers START")
        #mods = await self.style_modifiers()
        #logger.info("   ↳ style_modifiers END (t=%.3fs)", time.time() - start_ts)
        #mods_str = "; ".join(f"{k}={v:.2f}" for k, v in (mods or {}).items())
        #cr_str = "; ".join(f"{m}={self.change_rates.get(m,0.0):.2f}" for m in ("valence", "arousal", "stress", "anxiety"))
        #guide_str = ", ".join(unique_guides)

        sections: List[str] = [
            f"Your Name: {self.name}.",
            f"Your Age: {self.age} years old.",
            f"Your Gender: {self.gender}.",
            f"Your Zodiac: {self.zodiac}.",
            f"Your Temperament: {json.dumps(self.temperament, ensure_ascii=False, sort_keys=True, separators=(',',':'))}.",
            f"Your Sociality: {self.sociality}.",
            f"Your Archetypes: {', '.join(self.archetypes) or 'None'}.",
            f"Your Role: {self.role}.",
            #f"Your Mood & Character Modifiers: {guide_str}",
            #f"Mood State: {self.mood}",
            #f"Internal Metrics: {metrics_str}",
            #f"ChangeRates: {cr_str}",
            #f"Style Modifiers: {mods_str}",
        ]

        if want_any_memory and has_query:
            try:
                if query_emb is None:
                    t0 = time.perf_counter()
                    try:
                        query_emb = await asyncio.wait_for(
                            get_embedding(self._last_user_msg),
                            timeout=15.0
                        )
                        logger.info("openai.embedding t=%.3fs", time.perf_counter() - t0)
                    except Exception as e:
                        logger.warning("openai.embedding failed after %.3fs: %s",
                                       time.perf_counter() - t0, e)
                        query_emb = None
                else:
                    pass
            except Exception as e:
                logger.warning("to_prompt: embedding failed: %s", e)
                query_emb = None

        msg_hash_short = hashlib.md5((self._last_user_msg or "").encode("utf-8")).hexdigest()[:8]
        cache_key_memfu = (self._last_uid or 0, msg_hash_short)
        cached_memfu = self._memfu_local_cache.get(cache_key_memfu)

        past_cands: list = []
        present_cands: list = []
        future_cands: list = []
        if want_any_memory and has_query and query_emb is not None:
            try:
                past_c, pres_c, fut_c = await asyncio.wait_for(
                    asyncio.gather(
                        self.enhanced_memory.query_time(query_emb, event_type="past", top_k=5, uid=self._last_uid),
                        self.enhanced_memory.query_time(query_emb, event_type="present", top_k=5, uid=self._last_uid),
                        self.enhanced_memory.query_time(query_emb, event_type="future", top_k=5, uid=self._last_uid),
                    ),
                    timeout=10.0
                )
                past_cands, present_cands, future_cands = past_c, pres_c, fut_c
            except Exception as e:
                logger.warning("to_prompt: memory.query_time failed: %s", e)
                past_cands = present_cands = future_cands = []

        if want_any_memory:
            try:
                await self.ltm.ready()
                ltm_pick = await asyncio.wait_for(
                    self.ltm.pick_snippets(
                        uid=(self._last_uid or 0),
                        context=(self._last_user_msg or ""),
                        now_ts=time.time(),
                        turn_id=self.state_version,
                        query_vec=query_emb,
                    ),
                    timeout=3.0
                )
                if ltm_pick.get("fact"):
                    sections.append(f"USER.ProfileFacts: {ltm_pick['fact']}")
                if ltm_pick.get("boundary"):
                    sections.append(f"USER.UserBoundary: {ltm_pick['boundary']}")
                if ltm_pick.get("plan"):
                    sections.append(f"USER.Commitment: {ltm_pick['plan']}")
            except Exception:
                logger.debug("ltm.pick_snippets failed", exc_info=True)

        if want_any_memory and has_query and (query_emb is not None):
            try:
                prof_pairs = await asyncio.wait_for(
                    self.ltm.relevant_profile(
                        uid=(self._last_uid or 0),
                        context=(self._last_user_msg or ""),
                        top_n=int(getattr(settings, "PROFILE_MAX_ITEMS", 14)),
                        min_conf=float(getattr(settings, "PROFILE_MIN_CONF", 0.28)),
                        query_vec=query_emb,
                    ),
                    timeout=3.0
                )
                if prof_pairs:
                    sections.append("USER.Profile: " + "; ".join(f"{k}={v}" for k, v in prof_pairs))
                    sections.append("Rule: Do NOT conflate USER.Profile with your own attributes.")
                    try:
                        await asyncio.wait_for(
                            self.ltm.mark_profile_used(
                                uid=(self._last_uid or 0),
                                pairs=prof_pairs,
                                turn_id=self.state_version,
                                now_ts=time.time(),
                            ),
                            timeout=2.5
                        )
                    except Exception:
                        logger.debug("mark_profile_used failed", exc_info=True)
            except Exception:
                logger.debug("UserProfile assemble failed", exc_info=True)

        selected = {}
        if want_mem_followup:
            sim_thr = getattr(settings, "MEMORYFOLLOWUP_SIM_THRESHOLD", 0.60)
            try:
                uid0 = self._last_uid or 0
                trust_ema = float(self.attachments.get(uid0, {}).get("trust_ema", 0.5))
                sim_thr = min(0.95, max(0.50, sim_thr + 0.15 * max(0.0, 0.5 - trust_ema)))
            except Exception:
                pass
            now_iso = datetime.utcnow().isoformat() + "Z"
            logger.debug("   ↳ select_relevant_memories START")
            have_any = bool(past_cands or present_cands or future_cands)
            if have_any:
                if cached_memfu:
                    try:
                        ts, payload = cached_memfu
                        if (time.time() - ts) <= getattr(self, "_memfu_ttl", 3600.0):
                            selected = json.loads(payload)
                    except Exception:
                        selected = {}
                num_items = sum(len(x or []) for x in (past_cands, present_cands, future_cands))
                if not selected and (not use_llm_selector or num_items <= 6):
                    selected = self._select_relevant_memories_fast(
                        past_scored=past_cands, present_scored=present_cands, future_scored=future_cands,
                        per_cat=2, sim_thr=sim_thr
                    )
                if not selected:
                    try:
                        selected = await asyncio.wait_for(
                            self.select_relevant_memories(
                                now=now_iso,
                                context=self._last_user_msg or "",
                                candidates={
                                    "past": [t for t,_ in (past_cands or [])],
                                    "present": [t for t,_ in (present_cands or [])],
                                    "future":  [t for t,_ in (future_cands or [])],
                                }
                            ),
                            timeout=10.0
                        )
                    except Exception as e:
                        logger.warning("to_prompt: select_relevant_memories failed: %s", e)
                        selected = self._select_relevant_memories_fast(
                            past_scored=past_cands, present_scored=present_cands, future_scored=future_cands,
                            per_cat=2, sim_thr=sim_thr
                        )
                    finally:
                        logger.debug("   ↳ select_relevant_memories END (t=%.3fs)", time.time() - start_ts)

            if selected:
                try:
                    self._memfu_local_cache[cache_key_memfu] = (time.time(), json.dumps(selected, ensure_ascii=False))
                    self._prune_memfu_cache()
                except Exception:
                    pass
                past_map    = {_norm_key(t): sc for (t, sc) in (past_cands or [])}
                present_map = {_norm_key(t): sc for (t, sc) in (present_cands or [])}
                future_map  = {_norm_key(t): sc for (t, sc) in (future_cands or [])}
                past_orig    = {_norm_key(t): t for (t, _sc) in (past_cands or [])}
                present_orig = {_norm_key(t): t for (t, _sc) in (present_cands or [])}
                future_orig  = {_norm_key(t): t for (t, _sc) in (future_cands or [])}

                parts: list[str] = []
                def _clip(x: str, n: int = 240) -> str:
                    return x if len(x) <= n else (x[: n - 1] + "…")
                _trimmed = False
                if selected.get("past"):
                    _cnt = 0
                    for t in selected["past"]:
                        k = _norm_key(t)
                        if past_map.get(k, 0.0) >= sim_thr:
                            parts.append(f"past:{_clip(past_orig.get(k, t))}")
                            _cnt += 1
                            if _cnt >= 2:
                                _trimmed = True
                                break
                if selected.get("present"):
                    _cnt = 0
                    for t in selected["present"]:
                        k = _norm_key(t)
                        if present_map.get(k, 0.0) >= sim_thr:
                            parts.append(f"present:{_clip(present_orig.get(k, t))}")
                            _cnt += 1
                            if _cnt >= 2:
                                _trimmed = True
                                break
                if selected.get("future"):
                    _cnt = 0
                    for t in selected["future"]:
                        k = _norm_key(t)
                        if future_map.get(k, 0.0) >= sim_thr:
                            parts.append(f"future:{_clip(future_orig.get(k, t))}")
                            _cnt += 1
                            if _cnt >= 2:
                                _trimmed = True
                                break
                if parts:
                    sections.append("MemoryFollowUp=" + " | ".join(parts))
                if _trimmed:
                    logger.debug("MemoryFollowUp: trimmed to max 2 per category")

        if self.current_dominant:
            sections.append(f"DominantEmotion: {self.current_dominant}")

        if self.last_user_emotions:
            emotions_str = ", ".join(
                e if isinstance(e, str)
                else e.name if isinstance(e, Tone)
                else str(e)
                for e in self.last_user_emotions
            )
            sections.append(f"UserRecentEmotions: {emotions_str}")

        #if self._last_user_msg:
            #tone_sample = self._safe_snippet(self._last_user_msg)[:120]
            #sections.append(f"MimicUserStyle: {tone_sample}")

        if want_recall_snippet and has_query and (query_emb is not None):
            try:
                for text, score in await self.enhanced_memory.query(query_emb, top_k=3, uid=self._last_uid):
                    sections.append(f"MemoryHint[{score:.2f}]: {text}")
            except Exception as e:
                logger.warning("to_prompt: RecallPastSnippet failed: %s", e)
        elif want_recall_snippet and has_query:
            logger.debug("to_prompt: skip RecallPastSnippet because embedding is unavailable")

        if want_recall_snippet and has_query:
            try:
                q = (self._last_user_msg or "").lower()
                if q and hasattr(self, "_recent_sketch"):
                    def _sim(a: str, b: str) -> float:
                        sa = {t for t in re.findall(r"\w{3,}", a.lower())}
                        sb = {t for t in re.findall(r"\w{3,}", b.lower())}
                        if not sa or not sb:
                            return 0.0
                        inter = len(sa & sb)
                        union = len(sa | sb)
                        return inter / union if union else 0.0
                    weak_hits = []
                    for item in list(self._recent_sketch):
                        s = _sim(q, item.get("text",""))
                        if s >= float(getattr(settings, "MEM_RECENT_SKETCH_SIM_THR", 0.35)):
                            weak_hits.append((item["text"], s))
                    weak_hits.sort(key=lambda x: x[1], reverse=True)
                    for t, s in weak_hits[:2]:
                        sections.append(f"MemoryHint[{0.50 + 0.40*s:.2f}]: {t}")
            except Exception:
                logger.debug("weak-sketch recall failed", exc_info=True)

        result = "\n".join(sections)
        total = time.time() - start_ts
        logger.debug("✔ to_prompt END chat=%s version=%s len(sections)=%d t=%.3fs",
                     self.chat_id, self.state_version, len(sections), total)
        self._last_prompt_version = self.state_version
        self._last_prompt_guidelines = cache_key
        self._prompt_cache = result
        return result

    def apply_overrides(self, prefs: Optional[dict] = None, *, reset: bool = False) -> None:
        if reset or prefs is None:
            self.name = getattr(settings, "PERSONA_NAME", self.name)
            self.age = getattr(settings, "PERSONA_AGE", self.age)
            self.gender = getattr(settings, "PERSONA_GENDER", self.gender)
            self.zodiac = getattr(
                settings,
                "PERSONA_ZODIAC",
                getattr(settings, "PERSONA_ZODIAC", self.zodiac),
            )

            temp_json = getattr(
                settings,
                "PERSONA_TEMPERAMENT",
                getattr(settings, "PERSONA_TEMPERAMENT", ""),
            )
            try:
                self.temperament = json.loads(temp_json)
            except Exception:
                self.temperament = {
                    "sanguine": 0.4,
                    "choleric": 0.25,
                    "phlegmatic": 0.20,
                    "melancholic": 0.15,
                }

            self.sociality = "extrovert"
            try:
                arch_json = getattr(settings, "PERSONA_ARCHETYPES", '["Rebel","Jester","Sage"]')
                self.archetypes = json.loads(arch_json)
            except Exception:
                self.archetypes = ["Rebel","Jester","Sage"]
            self.role = getattr(settings, "PERSONA_ROLE", self.role)

            self.state_version += 1
            self._last_prompt_guidelines = None
            return

        if not isinstance(prefs, dict):
            return

        name = prefs.get("name")
        if isinstance(name, str) and name.strip():
            self.name = name.strip()[:64]

        age = prefs.get("age")
        try:
            ai = int(age)
            if 1 <= ai <= 120:
                self.age = ai
        except (TypeError, ValueError):
            pass

        gender = prefs.get("gender")
        if isinstance(gender, str) and gender in ("male", "female"):
            self.gender = gender

        z = prefs.get("zodiac")
        if isinstance(z, str) and z in ZODIAC_SET:
            self.zodiac = z

        t = _normalize_temperament(
            prefs.get("temperament"),
            getattr(
                settings,
                "PERSONA_TEMPERAMENT",
                getattr(settings, "PERSONA_TEMPERAMENT", "{}"),
            ),
        )
        self.temperament = t

        s = prefs.get("sociality")
        if isinstance(s, str) and s in SOCIALITY_SET:
            self.sociality = s

        a = prefs.get("archetypes")
        if isinstance(a, list):
            norm = []
            seen = set()
            for x in a:
                if not x:
                    continue
                v = str(x)
                if v in ARCHETYPES_SET and v not in seen:
                    seen.add(v)
                    norm.append(v)
                if len(norm) >= MAX_ARCH:
                    break
            if norm:
                self.archetypes = norm

        role = prefs.get("role")
        if isinstance(role, str) and role.strip():
            self.role = role.strip()[:1000]

        self.state_version += 1
        self._last_prompt_guidelines = None

    async def _ensure_rds(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        cur_id = id(loop)
        if (self._rds is None) or (self._rds_loop_id != cur_id):
            try:
                from app.core.memory import get_redis
                self._rds = get_redis()
                self._rds_loop_id = cur_id
            except Exception:
                self._rds = None
                self._rds_loop_id = None
        return self._rds

    async def _persist_attachment(self, uid: int) -> None:
        if not getattr(settings, "ATTACHMENT_PERSIST", False):
            return
        rds = await self._ensure_rds()
        if not rds:
            return
        rec = self.attachments.get(uid)
        if not rec:
            return
        key = f"attach:{self.chat_id}:{uid}"
        try:
            last_ts = float(rec.get("_persist_ts", 0.0))
        except Exception:
            last_ts = 0.0
        min_period = float(getattr(settings, "ATTACHMENT_PERSIST_MIN_PERIOD", 15.0))
        min_delta  = float(getattr(settings, "ATTACHMENT_PERSIST_MIN_DELTA", 0.01))
        if (time.time() - last_ts) < min_period and abs(rec.get("value", 0.0) - rec.get("_persist_value", 0.0)) < min_delta:
            return
        try:
            await rds.hset(key, mapping={
                "value": rec.get("value", 0.0),
                "vel": rec.get("vel", 0.0),
                "ts": rec.get("ts", 0.0),
                "rupture": rec.get("rupture", 0),
                "recovery": rec.get("recovery", 0.0),
                "rupture_until": rec.get("rupture_until", 0.0),
                "stage": rec.get("stage", ""),
                "born_ts": rec.get("born_ts", 0.0),
                "trust_ema": rec.get("trust_ema", 0.5),
                "style": rec.get("style", "secure"),
                "style_conf": rec.get("style_conf", 0.0),
                "pos_accum": rec.get("pos_accum", 0.0),
                "signals": json.dumps(rec.get("signals", {})),
            })
            ttl = int(getattr(settings, "ATTACHMENT_PERSIST_TTL_SECS", 30 * 24 * 3600))
            if ttl > 0:
                try:
                    await rds.expire(key, ttl)
                except Exception:
                    logger.debug("persist_attachment expire failed", exc_info=True)
            rec["_persist_ts"] = time.time()
            rec["_persist_value"] = rec.get("value", 0.0)
        except Exception:
            logger.debug("persist_attachment failed", exc_info=True)

    async def _load_attachment(self, uid: int) -> None:
        if not getattr(settings, "ATTACHMENT_PERSIST", False):
            return
        if uid in self.attachments:
            return
        rds = await self._ensure_rds()
        if not rds:
            return
        key = f"attach:{self.chat_id}:{uid}"
        try:
            data = await rds.hgetall(key)
            if data:
                def _num(x, default=0.0):
                    try:
                        return float(x.decode() if isinstance(x, (bytes, bytearray)) else x)
                    except Exception:
                        return default
                rec = {
                    "value": _num(data.get(b"value", 0.0)),
                    "vel": _num(data.get(b"vel", 0.0)),
                    "ts": _num(data.get(b"ts", 0.0)),
                    "rupture": int(_num(data.get(b"rupture", 0.0))),
                    "recovery": _num(data.get(b"recovery", 0.0)),
                    "rupture_until": _num(data.get(b"rupture_until", 0.0)),
                    "born_ts": _num(data.get(b"born_ts", 0.0)),
                    "trust_ema": _num(data.get(b"trust_ema", 0.5)),
                    "style_conf": _num(data.get(b"style_conf", 0.0)),
                    "pos_accum": _num(data.get(b"pos_accum", 0.0)),
                }
                try:
                    rec["stage"] = (
                        data.get(b"stage", b"").decode() if isinstance(data.get(b"stage"), (bytes, bytearray))
                        else (data.get("stage") or "")
                    )
                except Exception:
                    rec["stage"] = ""
                try:
                    rec["style"] = (
                        data.get(b"style", b"secure").decode()
                        if isinstance(data.get(b"style"), (bytes, bytearray)) else (data.get("style") or "secure")
                    )
                except Exception:
                    rec["style"] = "secure"
                try:
                    sig_raw = data.get(b"signals")
                    if sig_raw:
                        rec["signals"] = json.loads(sig_raw.decode() if isinstance(sig_raw, (bytes, bytearray)) else sig_raw)
                except Exception:
                    rec["signals"] = {"samples":0,"q":0,"apol":0,"clingy":0,"boundary":0}
                rec = self._ensure_attachment_defaults(rec, time.time())
                self.attachments[uid] = rec
        except Exception:
            logger.debug("load_attachment failed", exc_info=True)

    async def select_relevant_memories(
        self,
        now: str,
        context: str,
        candidates: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:

        candidates = {
            "past":    [t for t in (candidates.get("past") or []) if t],
            "present": [t for t in (candidates.get("present") or []) if t],
            "future":  [t for t in (candidates.get("future") or []) if t],
        }
        if not (candidates["past"] or candidates["present"] or candidates["future"]):
            return {"past": [], "present": [], "future": []}

        system_prompt = (
            "You are a memory selector.\n"
            "- Understand ANY language in the items below.\n"
            "- DO NOT translate or paraphrase anything.\n"
            "- Each category lists items as [index] text.\n"
            "- Select up to 2 indices per category that are most relevant right now.\n"
            "OUTPUT POLICY:\n"
            "- Return ONLY one minified JSON object with EXACT lowercase ASCII keys: past, present, future.\n"
            "- Each value must be an array of INTEGER indices (not strings). Use an empty array if none.\n"
            "- NO code fences, NO comments, NO prose.\n"
            "Example: {\"past\":[0,2],\"present\":[],\"future\":[1]}"
            f"\nCurrent time: {now}.\n"
            f"Conversation context: \"{context}\""
        )

        user_prompt = (
            "Return ONLY a single minified JSON object."
        )

        for cat, items in candidates.items():
            if items:
                listing = "\n".join(f"[{i}] {t}" for i, t in enumerate(items))
            else:
                listing = "(none)"
            system_prompt += f"\n{cat.capitalize()} (choose by index):\n{listing}\n"
        logger.debug("   ↳ select_relevant_memories → OpenAI call")

        selection_schema = {
            "type": "object",
            "properties": {
                "past": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "maxItems": 2,
                    "uniqueItems": True
                },
                "present": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "maxItems": 2,
                    "uniqueItems": True
                },
                "future":  {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "maxItems": 2,
                    "uniqueItems": True
                },
            },
            "required": ["past", "present", "future"],
            "additionalProperties": False,
        }
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    instructions=system_prompt,
                    input=user_prompt,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "memory_selection",
                            "schema": selection_schema,
                            "strict": True
                        }
                    },
                    temperature=0,
                    max_output_tokens=250,
                ),
                timeout=30.0,
            )
            logger.info(
                "openai.timing: responses.create model=%s input_chars=%d duration=%.3fs",
                settings.REASONING_MODEL, len(user_prompt), time.perf_counter() - t0
            )
            content = (_get_output_text(resp) or "").strip()
        except Exception:
            logger.debug("Error generating JSON memory selection", exc_info=True)
            return {
                "past":    candidates.get("past", [])[:2],
                "present": candidates.get("present", [])[:2],
                "future":  candidates.get("future", [])[:2],
            }

        try:
            if content.startswith("```"):
                content = content.strip("`")
                if "\n" in content:
                    content = content.split("\n", 1)[1]

            content = content.lstrip("\ufeff")

            if "{" in content and "}" in content:
                content = content[content.find("{"): content.rfind("}") + 1]

            sel_raw = json.loads(content)

            if isinstance(sel_raw, dict):
                sel_raw = {str(k).strip().lower(): v for k, v in sel_raw.items()}
            else:
                sel_raw = {}

            def _as_idx_list(x, n):
                seen = set(); out = []
                if isinstance(x, list):
                    for it in x:
                        try: idx = int(it)
                        except Exception: continue
                        if 0 <= idx < n and idx not in seen:
                            seen.add(idx); out.append(idx)
                        if len(out) == 2: break
                return out
                
            past_idx    = _as_idx_list(sel_raw.get("past"),    len(candidates.get("past",    [])))
            present_idx = _as_idx_list(sel_raw.get("present"), len(candidates.get("present", [])))
            future_idx  = _as_idx_list(sel_raw.get("future"),  len(candidates.get("future",  [])))
            sel = {
                "past":    [candidates["past"][i]    for i in past_idx]    if candidates.get("past")    else [],
                "present": [candidates["present"][i] for i in present_idx] if candidates.get("present") else [],
                "future":  [candidates["future"][i]  for i in future_idx]  if candidates.get("future")  else [],
            }
        except Exception:
            logger.debug("Memory selection JSON parse failed, fallback to top-2 each")
            sel = {
                "past":    candidates.get("past", [])[:2],
                "present": candidates.get("present", [])[:2],
                "future":  candidates.get("future", [])[:2],
            }
        return sel

    def _select_relevant_memories_fast(
        self,
        *,
        past_scored: list[tuple[str, float]] | None,
        present_scored: list[tuple[str, float]] | None,
        future_scored: list[tuple[str, float]] | None,
        per_cat: int = 2,
        sim_thr: float | None = None,
    ) -> dict[str, list[str]]:

        thr = float(sim_thr if sim_thr is not None else getattr(settings, "MEMORYFOLLOWUP_SIM_THRESHOLD", 0.60))
        def pick(ls: list[tuple[str,float]] | None) -> list[str]:
            if not ls:
                return []
            ordered = sorted(((t, float(s or 0.0)) for t, s in ls if t), key=lambda x: x[1], reverse=True)
            passed = [t for t, s in ordered if s >= thr][:per_cat]
            if passed:
                return passed
            return [ordered[0][0]] if ordered else []
        return {
            "past":    pick(past_scored or []),
            "present": pick(present_scored or []),
            "future":  pick(future_scored or []),
        }

    async def summary(self) -> str:
        t = self.temperament
        top_temp = max(t, key=t.get)
        low_temp = min(t, key=t.get)
        last_uid = getattr(self, "_last_uid", None)
        weight_pct = (
            f"{(self._decayed_weight(last_uid) * 100):.0f}%"
            if last_uid is not None
            else "N/A"
        )
        if last_uid is not None and last_uid in self.attachments:
            att_v = self.attachments[last_uid].get("value", 0.0)
            try:
                from .states import _attachment_label
                att_label = _attachment_label(att_v)
            except Exception:
                att_label = "N/A"
            attach_str = f"{att_label}:{att_v:.2f}"
        else:
            attach_str = "N/A"
        mem_count = await self.enhanced_memory.count_entries()
        return (
            f"{self.name} | Mood={self.mood} | V={self.state.get('valence', 0.0):.2f} "
            f"A={self.state.get('arousal', 0.0):.2f} E={self.state.get('energy',0.0):.2f} "
            f"S={self.state.get('stress', 0.0):.2f} Anx={self.state.get('anxiety',0.0):.2f} | "
            f"Dom={self.current_dominant or 'None'} | "
            f"Sa{t.get('sanguine',0)*100:.0f}% Ch{t.get('choleric',0)*100:.0f}% "
            f"Ph{t.get('phlegmatic',0)*100:.0f}% Me{t.get('melancholic',0)*100:.0f}% | "
            f"TopTemp={top_temp}:{t.get(top_temp,0.0):.2f} LowTemp={low_temp}:{t.get(low_temp,0.0):.2f} | "
            f"Weight={weight_pct} | Attach={attach_str} | MemEntries={mem_count}"
        )
    
    async def process_interaction(
        self,
        uid: int,
        text: str,
        user_gender: str | None = None
    ) -> None:

        try:
            self._spawn(lambda: _mem_record_activity(self.chat_id, uid))
        except Exception:
            logger.debug("core.memory.record_activity spawn failed", exc_info=True)
        try:
            await self.ltm.record_activity(uid)
        except Exception:
            logger.debug("ltm.record_activity error", exc_info=True)
        
        await self._ensure_background_started()
        return await _process_interaction_impl(self, uid, text, user_gender=user_gender,)

    def _prune_memfu_cache(self) -> None:
        try:
            now = time.time()
            ttl = getattr(self, "_memfu_ttl", 3600.0)
            cap = getattr(self, "_memfu_cap", 256)
            dead = [k for k, (ts, _payload) in self._memfu_local_cache.items() if (now - ts) > ttl]
            for k in dead:
                self._memfu_local_cache.pop(k, None)
            if len(self._memfu_local_cache) > cap:
                drop = max(1, cap // 10)
                for k, _ in sorted(self._memfu_local_cache.items(), key=lambda kv: kv[1][0])[:drop]:
                    self._memfu_local_cache.pop(k, None)
        except Exception:
            pass

    async def close(self) -> None:
        t = getattr(self, "_bg_task", None)
        if t and not t.done():
            try:
                n = int(getattr(settings, "BG_WORKER_CONCURRENCY", 8))
            except Exception:
                n = 8
            try:
                q = getattr(self, "_bg_queue", None)
                if q is not None and n > 0:
                    sent = 0
                    for _ in range(n):
                        try:
                            q.put_nowait((None, None, None, None, None))
                            sent += 1
                        except asyncio.QueueFull:
                            break
                    if sent:
                        timeout = float(getattr(settings, "BG_DRAIN_TIMEOUT", 1.5))
                        try:
                            await asyncio.wait_for(t, timeout=timeout)
                        except asyncio.TimeoutError:
                            pass
            except Exception:
                logger.debug("persona.close drain failed", exc_info=True)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        wt = getattr(self, "_worker_task", None)
        if wt and not wt.done():
            wt.cancel()
            try:
                await wt
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        self._bg_started = False
        self._bg_task = None
        pending = [x for x in list(self._spawned_tasks) if not x.done()]
        for x in pending:
            x.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            close_coro = getattr(self.ltm, "close", None)
            if callable(close_coro):
                res = close_coro()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            pass

    def _safe_snippet(self, text: str | bytes) -> str:
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", "ignore")
        try:
            text = unicodedata.normalize("NFKC", text)
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            return text.replace("\n", " ").strip()

    T = TypeVar("T")
    def _spawn(
        self,
        fn_or_coro: Callable[[], Coroutine[Any, Any, T]] | Coroutine[Any, Any, T] | asyncio.Task[T],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[T]:

        if isinstance(fn_or_coro, asyncio.Task):
            t: asyncio.Task[T] = fn_or_coro
        elif inspect.iscoroutine(fn_or_coro):
            raise TypeError("_spawn(): pass a callable (async function) or Task, not a coroutine object")
        elif callable(fn_or_coro):
            if inspect.iscoroutinefunction(fn_or_coro):
                t = asyncio.create_task(fn_or_coro(), name=name or "spawned-task")
            else:
                async def _runner_call_maybe_coro(fn: Callable[[], Any]):
                    res = await asyncio.to_thread(fn)
                    if inspect.iscoroutine(res):
                        return await res
                    return res
                t = asyncio.create_task(_runner_call_maybe_coro(fn_or_coro), name=name or "spawned-task")
        else:
            raise TypeError(f"_spawn(): expected coroutine/callable/Task, got {type(fn_or_coro)!r}")

        self._spawned_tasks.add(t)

        def _done(fut: asyncio.Task[T]) -> None:
            self._spawned_tasks.discard(fut)
            if fut.cancelled():
                return
            try:
                exc = fut.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error("Uncaught exception in background task %r", fut, exc_info=(type(exc), exc, exc.__traceback__))

        t.add_done_callback(_done)
        return t

    def spawn_coro(self, coro_fn: Callable[..., Coroutine[Any, Any, T]], *args, name: Optional[str] = None, **kwargs) -> asyncio.Task[T]:

        if not inspect.iscoroutinefunction(coro_fn):
            raise TypeError("spawn_coro expects an async function")
        async def _runner():
            return await coro_fn(*args, **kwargs)
        return self._spawn(_runner, name=name or "spawned-coro")