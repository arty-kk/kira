cat >app/emo_engine/persona/core.py<< EOF
#app/emo_engine/persona/core.py
from __future__ import annotations

import asyncio, json, random, time, threading

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, ClassVar
from math import exp

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry

from .memory import (
    MemoryEntry, _restore, _persist
)
from .states import (
    _recompute_rates, process_interaction, _blend_metric, _update_mood_label,
    _decayed_weight, _compute_salience, _update_weight,
)
from .utils.emotion_math import (
    _compute_secondary, 
    _clamp as _global_clamp,
    _compute_tertiary,
)
from .utils.text_analyzer import TextAnalyzer
from .stylers.modifiers import style_modifiers
from .stylers.guidelines import (
    style_guidelines, _gather_extras
)
from .constants.labels import EMO_LABEL_MAP as EMO_LABELS
from .constants.emotions import (  
    ALL_METRICS, PRIMARY_EMOTIONS, SECONDARY_EMOTIONS,
    TERTIARY_EMOTIONS, DYAD_KEYS, TRIAD_KEYS,
    COGNITIVE_METRICS, EXTRA_TRIGGER_METRICS,
)

@dataclass
class Persona:
    chat_id: int
    name: str = settings.BOT_PERSONA_NAME
    gender: str = settings.BOT_PERSONA_GENDER
    age: int = settings.BOT_PERSONA_AGE
    role: str = settings.BOT_PERSONA_ROLE
    origin: str = settings.BOT_PERSONA_ORIGIN
    zodiac: str = settings.PERSONA_ZODIAC_SIGN
    temperament: Dict[str, float] = field(
        default_factory=lambda: json.loads(settings.PERSONA_TEMPERAMENT_DISTRIBUTION_JSON)
    )
    state: Dict[str, float] = field(init=False, default_factory=dict)
    change_rates: Dict[str, float] = field(init=False, default_factory=dict)
    mood: str = "steady"
    user_weights: Dict[int, List[float]] = field(init=False, default_factory=dict)
    memory_entries: deque = field(init=False, default_factory=lambda: deque(maxlen=settings.MEMORY_MAX_ENTRIES))
    _lock: "asyncio.Lock" = field(init=False, repr=False, compare=False, default=None)
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
    process_interaction = process_interaction
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
    _gather_extras = _gather_extras
    _restore = _restore
    _persist = _persist
    _clamp = staticmethod(_global_clamp)
    

    _EMO_LABEL_MAP: ClassVar[dict[str, str]] = EMO_LABELS


    def __post_init__(self) -> None:
        self._rng = random.Random(self.chat_id)
        self._mods_lock = threading.Lock()
        self._restored_evt = asyncio.Event()
        self._in_peak = False
        self._last_valence_peak_ts = time.time()
        self._style_mods_version = -1
        self._recompute_rates()
        
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
        scale = 1.0
        self.state = {
            m: 0.5 + (normed[m] - 0.5) * scale
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
        self.ema = {e: 0.5 for e in PRIMARY_EMOTIONS + ["arousal", "energy", "fatigue"] + extra_ema}
        self.dominant_threshold = settings.EMO_THRESHOLD_DOMINANT
        self.dominant_locked = False
        self.current_dominant = None
        self._last_prompt_version = -1
        self._last_prompt_guidelines = None
        self._prompt_cache = ""
        self._loop_id = id(asyncio.get_event_loop())
        self._lock = asyncio.Lock()
        self.last_user_emotions = []
        self._just_pushed_back = False
        self._cached_style_modifiers = {}
        self._mods_cache = {}
        self._last_uid = None
        self.flowstate = 0.0
        self._last_mood_change_ts = time.time()
        self._prev_mood = self.mood
        self._text_analyzer = TextAnalyzer()

        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(self._restore())
        else:
            loop.run_until_complete(self._restore())

    async def analyze_text(self, text: str) -> Dict[str, float]:
        return await self._text_analyzer.analyze_text(text)

    def _k_state(self) -> str:
        return f"persona:{self.chat_id}:state"


    def _k_weights(self) -> str:
        return f"persona:{self.chat_id}:weights"


    def _k_memory(self) -> str:
        return f"persona:{self.chat_id}:memory_entries"


    async def generate_transition(self, context: str) -> str:

        prompt = (
            "You are a conversational assistant. "
            "Provide ONE short transition phrase (1–4 words) that smoothly leads into your reply, given this user message:\n"
            f"\"{context}\""
        )
        mods = getattr(self, "_mods_cache", None) or self.style_modifiers()
        temp = 0.5 + 0.5 * mods.get("creativity_mod", 0.5)
        resp = await _call_openai_with_retry(
            model=settings.BASE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temp,
            max_tokens=8,
        )
        return resp.choices[0].message.content.strip()


    def tweak(self, knob: str, delta: float) -> None:
        if knob not in COGNITIVE_METRICS:
            raise ValueError(f"Unknown knob: {knob}")
        self.state[knob] = self._clamp(
            self.state[knob] + delta,
            0.0,
            1.0,
        )


    def to_prompt(self, guidelines: List[str]) -> str:

        s = self.state

        mods = self.style_modifiers()
        guide_str_key = ",".join(guidelines)
        if self.state_version == self._last_prompt_version and guide_str_key == self._last_prompt_guidelines:
            return self._prompt_cache

        metrics_keys = [
            "valence_mod",
            "arousal_mod",
            "energy_mod",
            "fatigue_mod",
            "engagement_mod",
            "empathy_mod",
            "sexual_arousal_mod",
            "flirtation_mod",
            "sarcasm_mod",
            "aggressiveness_mod",
            "enthusiasm_mod",
            "creativity_mod",
            "profanity_mod",
            "joy_mod",
            "sadness_mod",
            "confidence_mod",
            "precision_mod",
            "anger_mod",
            "fear_mod",
            "disgust_mod",
            "surprise_mod",
            "anticipation_mod",
            "trust_mod",
            "stress_mod",
            "anxiety_mod",
            "civility_mod",
            "optimism_mod",
            "gloom_mod",
            "ecstasy_mod",
            "cheerfulness_mod",
            "loneliness_mod",
            "despair_mod",
            "friendliness_mod",
            "annoyance_mod",
            "irritation_mod",
            "astonishment_mod",
            "admiration_mod",
            "affection_mod",
            "embarrassment_mod",
            "guilt_mod",
            "charisma_mod",
            "authority_mod",
            "lustful_excitement_mod",
            "creative_collaboration_mod",
            *[f"{m}_mod" for m in COGNITIVE_METRICS]
        ]
        uniq_keys = dict.fromkeys(metrics_keys)
        metrics_str = "; ".join(f"{k}={s.get(k, 0.0):.2f}" for k in uniq_keys)

        mods_str = "; ".join(f"{k}={v:.2f}" for k, v in mods.items())
        cr_str = "; ".join(f"{m}={self.change_rates.get(m,0.0):.2f}" for m in ("valence", "arousal", "stress", "anxiety"))
        guide_str = ", ".join(guidelines)

        sections: List[str] = [
            f"Your Name: {self.name} — {self.age}-year-old.",
            f"Your Gender: {self.gender}.",
            f"Your Origin & Background: {self.origin}.",
            f"Your Zodiac: {self.zodiac}.",
            f"Your Temperament: {self.temperament}.",
            f"Your Mood State: {self.mood}",
            f"Internal Metrics: {metrics_str}",
            f"ChangeRates: {cr_str}",
            f"Style Modifiers: {mods_str}",
            f"Style Guidelines: {guide_str}",
        ]

        if self.current_dominant:
            sections.append(f"DominantEmotion: {self.current_dominant}")

        if self.last_user_emotions:
            sections.append(f"UserRecentEmotions: {', '.join(self.last_user_emotions)}")

        if self._last_user_msg:
            tone_sample = self._safe_snippet(self._last_user_msg)[:120]
            sections.append(f"MimicUserTone: {tone_sample}")

        if "RecallPastSnippet" in guidelines and self.memory_entries:
            target = {e: self.state.get(e, 0.0) for e in PRIMARY_EMOTIONS}
            def dist(entry: MemoryEntry) -> float:
                return sum(
                    (entry.readings.get(e, 0.0) - target[e]) ** 2
                    for e in PRIMARY_EMOTIONS
                )
            best = min(self.memory_entries, key=dist)
            sections.append(f"ConversationMemoryHint: {best.snippet}")

        if self.memory_entries:
            now = time.time()
            top = sorted(
                self.memory_entries,
                key=lambda e: e.salience * exp(-settings.MEMORY_SALIENCE_DECAY_RATE * (now - e.timestamp)),
                reverse=True,
            )[:2]
            for e in top:
                sections.append(f"Memory: {e.snippet}")

        result = "\n".join(sections)
        self._last_prompt_version = self.state_version
        self._last_prompt_guidelines = guide_str_key
        self._prompt_cache = result
        return result


    def summary(self) -> str:
        t = self.temperament
        top_temp = max(t, key=t.get)
        low_temp = min(t, key=t.get)
        last_uid = getattr(self, "_last_uid", None)
        weight_pct = (
            f"{(self._decayed_weight(last_uid) * 100):.0f}%"
            if last_uid is not None
            else "N/A"
        )
        return (
            f"{self.name} | Mood={self.mood} | V={self.state['valence']:.2f} "
            f"A={self.state['arousal']:.2f} E={self.state.get('energy',0.0):.2f} "
            f"S={self.state['stress']:.2f} Anx={self.state['anxiety']:.2f} | "
            f"Dom={self.current_dominant or 'None'} | "
            f"Sa{t['sanguine']*100:.0f}% Ch{t['choleric']*100:.0f}% "
            f"Ph{t['phlegmatic']*100:.0f}% Me{t['melancholic']*100:.0f}% | "
            f"TopTemp={top_temp}:{t[top_temp]:.2f} LowTemp={low_temp}:{t[low_temp]:.2f} | "
            f"Weight={weight_pct} | MemEntries={len(self.memory_entries)}"
        )


    def _safe_snippet(self, text: str) -> str:
        return (
            text.encode("utf-8")[:240]
            .decode("utf-8", "ignore")
            .replace("\n", " ")
        )
EOF