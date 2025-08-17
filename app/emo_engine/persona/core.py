cat >app/emo_engine/persona/core.py<< EOF
#app/emo_engine/persona/core.py
from __future__ import annotations

import asyncio
import json
import random
import time
import logging
import re
import unicodedata

from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, ClassVar
from types import MethodType

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry

from .memory import PersonaMemory, get_embedding
from .states import (
    _recompute_rates, process_interaction as _process_interaction_impl,
    _blend_metric, _update_mood_label, _bg_worker,
    _decayed_weight, _compute_salience, _update_weight,
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

logger = logging.getLogger(__name__)


@dataclass
class Persona:
    chat_id: int
    name: str = settings.BOT_PERSONA_NAME
    gender: str = settings.BOT_PERSONA_GENDER
    age: int = settings.BOT_PERSONA_AGE
    origin: str = settings.BOT_PERSONA_ORIGIN
    zodiac: str = settings.BOT_PERSONA_ZODIAC
    temperament: Dict[str, float] = field(
        default_factory=lambda: json.loads(settings.BOT_PERSONA_TEMPERAMENT)
    )
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
    flowstate: float = field(init=False, default=0.0)
    _style_mods_version: int = field(init=False, default=-1)
    _last_user_msg: str = field(init=False, default="")
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
        self._rng = random.Random(self.chat_id)
        self._restored_evt = asyncio.Event()
        self._in_peak = False
        self._last_valence_peak_ts = time.time()
        self._style_mods_version = -1
        for fn in (
            _recompute_rates, _blend_metric, _update_mood_label,
            _compute_secondary, _compute_tertiary, _bg_worker,
            _decayed_weight, _compute_salience, _update_weight,
        ):
            setattr(self, fn.__name__, MethodType(fn, self))

        self.style_modifiers = MethodType(style_modifiers, self)
        self.style_guidelines = MethodType(style_guidelines, self)
        self._recompute_rates()
        
        try:
            self._loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            self._loop_id = None

        if self.change_rates:
            min_rate = min(self.change_rates.values())
            max_rate = max(self.change_rates.values())
        else:
            min_rate = max_rate = 0.5
        span = max_rate - min_rate
        MIN_SPAN = 0.2
        if span < MIN_SPAN:
            mid = (max_rate + min_rate) / 2
            min_rate = mid - MIN_SPAN/2
            max_rate = mid + MIN_SPAN/2
            span = MIN_SPAN
        normed = {
            m: self._clamp((self.change_rates.get(m, 0.0) - min_rate) / span, 0.0, 1.0)
            for m in ALL_METRICS
        }
        center = settings.EMO_INITIAL_CENTER
        scale  = settings.EMO_INITIAL_SCALE
        self.state = {
            m: center + (normed[m] - 0.5) * scale
            for m in ALL_METRICS
        }
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
        self.ema = {e: 0.5 for e in PRIMARY_EMOTIONS + ["valence", "arousal", "energy", "fatigue"] + extra_ema}
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
        self.enhanced_memory = PersonaMemory()
        asyncio.create_task(self._start_bg_worker())
        asyncio.create_task(self._notify_ready())
        self._last_mood_change_ts = time.time()
        self._prev_mood = self.mood
        self._text_analyzer = TextAnalyzer()


    async def analyze_text(self, text: str) -> Dict[str, float]:
        return await self._text_analyzer.analyze_text(text)


    async def _notify_ready(self) -> None:
        await self.enhanced_memory.ready()
        self._restored_evt.set()


    async def _start_bg_worker(self) -> None:
        await self.enhanced_memory.ready()
        await self._bg_worker()


    def tweak(self, knob: str, delta: float) -> None:
        if knob not in COGNITIVE_METRICS:
            raise ValueError(f"Unknown knob: {knob}")
        self.state[knob] = self._clamp(
            self.state[knob] + delta,
            0.0,
            1.0,
        )


    async def to_prompt(self, guidelines: List[str]) -> str:

        def _norm_key(s: str) -> str:
            if not isinstance(s, str):
                s = str(s)
            s = unicodedata.normalize("NFKC", s)
            s = s.strip().strip('"\'')

            return re.sub(r"\s+", " ", s)

        
        start_ts = time.time()
        guide_names = ",".join(g.name if hasattr(g, "name") else str(g) for g in guidelines)
        logger.info("▶ to_prompt START chat=%s version=%s guides=%s",
                    self.chat_id, self.state_version, guide_names)

        s = self.state

        norm_guides   = [g.name if hasattr(g, "name") else str(g) for g in guidelines]
        guide_str_key = ",".join(norm_guides)
        if self.state_version == self._last_prompt_version and guide_str_key == self._last_prompt_guidelines:
            return self._prompt_cache
        want_mem_followup = "MemoryFollowUp" in norm_guides

        #metrics_str = "; ".join(f"{k}={s.get(k, 0.0):.2f}" for k in all_key_mods)

        #logger.info("   ↳ style_modifiers START")
        #mods = await self.style_modifiers()
        #logger.info("   ↳ style_modifiers END (t=%.3fs)", time.time() - start_ts)
        #mods_str = "; ".join(f"{k}={v:.2f}" for k, v in (mods or {}).items())
        #cr_str = "; ".join(f"{m}={self.change_rates.get(m,0.0):.2f}" for m in ("valence", "arousal", "stress", "anxiety"))
        guide_str = ", ".join(norm_guides)

        sections: List[str] = [
            f"Your Name: {self.name}.",
            f"Your Gender: {self.gender}.",
            f"Your Zodiac: {self.zodiac}.",
            f"Your Temperament: {self.temperament}.",
            #f"Origin & Background: {self.origin}.",
            #f"Mood State: {self.mood}",
            #f"Internal Metrics: {metrics_str}",
            #f"ChangeRates: {cr_str}",
            #f"Style Modifiers: {mods_str}",
            f"Style Guidelines: {guide_str}",
        ]

        if self._last_user_msg:
            try:
                query_emb = await asyncio.wait_for(
                    get_embedding(self._last_user_msg),
                    timeout=15.0
                )
            except Exception as e:
                logger.warning("to_prompt: embedding failed: %s", e)
                query_emb = (b"\x00" * 4) * settings.EMBED_DIM
        else:
            query_emb = (b"\x00" * 4) * settings.EMBED_DIM

        try:
            past_c, pres_c, fut_c = await asyncio.wait_for(
                asyncio.gather(
                    self.enhanced_memory.query_time(query_emb, event_type="past",    top_k=5),
                    self.enhanced_memory.query_time(query_emb, event_type="present", top_k=5),
                    self.enhanced_memory.query_time(query_emb, event_type="future",  top_k=5),
                ),
                timeout=10.0
            )
            past_cands, present_cands, future_cands = past_c, pres_c, fut_c
        except Exception as e:
            logger.warning("to_prompt: memory.query_time failed: %s", e)
            past_cands = present_cands = future_cands = []

        now_iso = datetime.utcnow().isoformat() + "Z"
        logger.info("   ↳ select_relevant_memories START")
        past_map    = {_norm_key(t): sc for (t, sc) in (past_cands or [])}
        present_map = {_norm_key(t): sc for (t, sc) in (present_cands or [])}
        future_map  = {_norm_key(t): sc for (t, sc) in (future_cands or [])}
        past_orig    = {_norm_key(t): t for (t, _sc) in (past_cands or [])}
        present_orig = {_norm_key(t): t for (t, _sc) in (present_cands or [])}
        future_orig  = {_norm_key(t): t for (t, _sc) in (future_cands or [])}
        sim_thr = getattr(settings, "MEMORYFOLLOWUP_SIM_THRESHOLD", 0.60)
        try:
            selected = await asyncio.wait_for(
                self.select_relevant_memories(
                    now=now_iso,
                    context=self._last_user_msg or "",
                    candidates={
                        "past": [t for t,_ in (past_cands or [])],
                        "present": [t for t,_ in (present_cands or [])],
                        "future": [t for t,_ in (future_cands or [])],
                    }
                ),
                timeout=10.0
            )
        except Exception as e:
            logger.warning("to_prompt: select_relevant_memories failed: %s", e)
            selected = {}
        finally:
            logger.info("   ↳ select_relevant_memories END (t=%.3fs)", time.time() - start_ts)

        if want_mem_followup and selected:
            parts: list[str] = []
            if selected.get("past"):
                for t in selected["past"]:
                    k = _norm_key(t)
                    if past_map.get(k, 0.0) >= sim_thr:
                        parts.append(f"past:{past_orig.get(k, t)}")
                        break
            if selected.get("present"):
                for t in selected["present"]:
                    k = _norm_key(t)
                    if present_map.get(k, 0.0) >= sim_thr:
                        parts.append(f"present:{present_orig.get(k, t)}")
                        break
            if selected.get("future"):
                for t in selected["future"]:
                    k = _norm_key(t)
                    if future_map.get(k, 0.0) >= sim_thr:
                        parts.append(f"future:{future_orig.get(k, t)}")
                        break
            if parts:
                sections.append("MemoryFollowUp=" + " | ".join(parts))

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

        if self._last_user_msg:
            tone_sample = self._safe_snippet(self._last_user_msg)[:120]
            sections.append(f"MimicUserTone: {tone_sample}")

        if "RecallPastSnippet" in norm_guides and self._last_user_msg:
            try:
                hint_emb = await asyncio.wait_for(
                    get_embedding(self._last_user_msg),
                    timeout=15.0
                )
                for text, score in await self.enhanced_memory.query(hint_emb, top_k=3):
                    sections.append(f"MemoryHint[{score:.2f}]: {text}")
            except Exception as e:
                logger.warning("to_prompt: RecallPastSnippet failed: %s", e)

        result = "\n".join(sections)
        total = time.time() - start_ts
        logger.info("✔ to_prompt END chat=%s version=%s len(sections)=%d t=%.3fs",
                    self.chat_id, self.state_version, len(sections), total)
        self._last_prompt_version = self.state_version
        self._last_prompt_guidelines = guide_str_key
        self._prompt_cache = result
        return result


    async def select_relevant_memories(
        self,
        now: str,
        context: str,
        candidates: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:

        sys_msg = (
            f"You are a memory manager. Current time: {now}.\n"
            f"Conversation context: \"{context}\"\n"
            "Below are lists of candidate user events by category:\n"
        )
        for cat, items in candidates.items():
            joined = "\n  - ".join(items) if items else "(none)"
            sys_msg += f"{cat.capitalize()}:\n  - {joined}\n"
        sys_msg += (
            "\nSelect up to 2 items per category that are most relevant to mention right now.\n"
            "Output JSON of the form:\n"
            '{"past": ["...","..."], "present": ["..."], "future": ["..."]}'
        )
        logger.info("   ↳ select_relevant_memories → OpenAI call")
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.REASONING_MODEL,
                messages=[
                    {"role": "system",  "content": sys_msg},
                    {"role": "user",    "content": "Please respond with the JSON selection only."},
                ],
                max_completion_tokens=300,
                temperature=0.0,
            ),
            timeout=30.0,
        )
        content = resp.choices[0].message.content.strip()
        logger.info("   ↳ select_relevant_memories response received (chars=%d)", len(content))
        try:
            sel: Dict[str, List[str]] = json.loads(content)
        except Exception:
            logger.warning("Memory selection JSON parse failed, fallback to top-2 each")
            sel = {
                "past":    candidates["past"][:2],
                "present": candidates["present"][:2],
                "future":  candidates["future"][:2],
            }
        return sel


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
        mem_count = await self.enhanced_memory.count_entries()
        return (
            f"{self.name} | Mood={self.mood} | V={self.state.get('valence', 0.0):.2f} "
            f"A={self.state.get('arousal', 0.0):.2f} E={self.state.get('energy',0.0):.2f} "
            f"S={self.state.get('stress', 0.0):.2f} Anx={self.state.get('anxiety',0.0):.2f} | "
            f"Dom={self.current_dominant or 'None'} | "
            f"Sa{t['sanguine']*100:.0f}% Ch{t['choleric']*100:.0f}% "
            f"Ph{t['phlegmatic']*100:.0f}% Me{t['melancholic']*100:.0f}% | "
            f"TopTemp={top_temp}:{t[top_temp]:.2f} LowTemp={low_temp}:{t[low_temp]:.2f} | "
            f"Weight={weight_pct} | MemEntries={mem_count}"
        )
    

    async def process_interaction(
        self,
        uid: int,
        text: str,
        user_gender: str | None = None
    ) -> None:
        
        return await _process_interaction_impl(self, uid, text, user_gender=user_gender,)


    def _safe_snippet(self, text: str | bytes) -> str:
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", "ignore")
        return text.replace("\n", " ")
EOF