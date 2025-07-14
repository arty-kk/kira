cat >app/emo_engine/persona/stylers/guidelines.py<< EOF
#app/emo_engine/persona/stylers/guidelines.py
from __future__ import annotations

import logging, random, secrets

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Set, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from ..utils.emotion_math import _sigmoid


class Tone(Enum):
    Joyful = auto()
    Melancholic = auto()
    Angry = auto()
    Fearful = auto()
    Disgusted = auto()
    Surprised = auto()
    Trusting = auto()
    Optimistic = auto()
    Gloomy = auto()
    Ecstatic = auto()
    Cheerful = auto()
    Lonely = auto()
    Despairing = auto()
    Annoyed = auto()
    Irritated = auto()
    Astonished = auto()
    Admiring = auto()
    Affectionate = auto()
    Lustful = auto()
    Witty = auto()
    Sexual = auto()
    Collaborative = auto()
    Civil = auto()
    Aggressive = auto()
    Eager = auto()
    Expectant = auto()
    Empathetic = auto()
    Engaged = auto()
    Confident = auto()
    Technical = auto()
    Playful = auto()
    Sarcastic = auto()
    Flirty = auto()
    Blunt = auto()
    Energetic = auto()
    Weary = auto()
    FlowState = auto()
    Stressed = auto()
    Anxious = auto()
    Enthusiastic = auto()
    Excited = auto()
    BurntOut = auto()
    Exhausted = auto()
    Euphoric = auto()
    Collapsed = auto()
    Friendly = auto()
    Neutral = auto()
    Confused = auto()
    Embarrassed = auto()
    Guilty = auto()
    Charismatic = auto()
    Persuasive = auto()
    Patient = auto()
    Authoritative = auto()
    

TONE_MAP: Dict[str, Tone] = {
    "neutral_mod": Tone.Neutral,
    "charisma_mod": Tone.Charismatic,
    "joy_mod": Tone.Joyful,
    "sadness_mod": Tone.Melancholic,
    "anger_mod": Tone.Angry,
    "fear_mod": Tone.Fearful,
    "disgust_mod": Tone.Disgusted,
    "surprise_mod": Tone.Surprised,
    "trust_mod": Tone.Trusting,
    "optimism_mod": Tone.Optimistic,
    "gloom_mod": Tone.Gloomy,
    "ecstasy_mod": Tone.Ecstatic,
    "cheerfulness_mod": Tone.Cheerful,
    "loneliness_mod": Tone.Lonely,
    "despair_mod": Tone.Despairing,
    "annoyance_mod": Tone.Annoyed,
    "irritation_mod": Tone.Irritated,
    "astonishment_mod": Tone.Astonished,
    "admiration_mod": Tone.Admiring,
    "affection_mod": Tone.Affectionate,
    "lustful_excitement_mod": Tone.Lustful,
    "sexual_arousal_mod": Tone.Sexual,
    "creative_collaboration_mod": Tone.Collaborative,
    "civility_mod": Tone.Civil,
    "aggressiveness_mod": Tone.Aggressive,
    "curiosity_mod": Tone.Eager,
    "anticipation_mod": Tone.Expectant,
    "empathy_mod": Tone.Empathetic,
    "engagement_mod": Tone.Engaged,
    "confidence_mod": Tone.Confident,
    "precision_mod": Tone.Technical,
    "humor_mod": Tone.Playful,
    "sarcasm_mod": Tone.Sarcastic,
    "flirtation_mod": Tone.Flirty,
    "profanity_mod": Tone.Blunt,
    "energy_mod": Tone.Energetic,
    "fatigue_mod": Tone.Weary,
    "flowstate_mod": Tone.FlowState,
    "stress_mod": Tone.Stressed,
    "anxiety_mod": Tone.Anxious,
    "enthusiasm_mod": Tone.Enthusiastic,
    "excitement_mod": Tone.Excited,
    "burnout_mod": Tone.BurntOut,
    "exhaustion_mod": Tone.Exhausted,
    "euphoria_mod": Tone.Euphoric,
    "collapse_mod": Tone.Collapsed,
    "friendliness_mod": Tone.Friendly,
    "confusion_mod": Tone.Confused,
    "embarrassment_mod": Tone.Embarrassed,
    "guilt_mod": Tone.Guilty,
    "persuasion_mod": Tone.Persuasive,
    "authority_mod": Tone.Authoritative,
    "wit_mod": Tone.Witty,
    "patience_mod":  Tone.Patient,
}


@dataclass(frozen=True)
class RhetoricalDevice:

    name: str
    metric_key: str
    base_prob: float = 0.2
    exclusive_with: Tuple[str, ...] = field(default_factory=tuple)

    def should_apply(self, metric_val: float, rng: random.Random) -> bool:
        
        x = max(0.0, min(1.0, (metric_val - 0.0) / (1.0 - 0.0)))
        k = 6.0
        sig = 1.0 / (1.0 + pow(2.71828, -k * (x - 0.5)))
        prob = self.base_prob * (0.2 + 0.8 * sig)
        return rng.random() < prob


EXTRA_DEVICES: Tuple[RhetoricalDevice, ...] = (
    #  ядро разговорности 
    RhetoricalDevice("VarySentenceLength",    "energy_mod",      0.55),
    RhetoricalDevice("UseFollowUpQuestion",   "engagement_mod",  0.26),
    RhetoricalDevice("AskClarifyingQuestion", "confusion_mod",   0.22),
    RhetoricalDevice("UseMetaphors",          "creativity_mod",  0.30,
                    exclusive_with=("UseSymbolism","UseSimiles")),
    RhetoricalDevice("UseAnalogies",          "precision_mod",   0.27,
                    exclusive_with=("UseSimiles",)),
    
    #  мягкие стилистические штрихи 
    RhetoricalDevice("UseVividLanguage",      "creativity_mod",  0.35),
    RhetoricalDevice("PopCultureRefs",        "curiosity_mod",   0.25,
                    exclusive_with=("CulturalRefs",)),
    RhetoricalDevice("CulturalRefs",          "creativity_mod",  0.20,
                    exclusive_with=("PopCultureRefs",)),
    RhetoricalDevice("UseSimiles",            "creativity_mod",  0.28,
                    exclusive_with=("UseAnalogies","UseMetaphors")),
    RhetoricalDevice("UseContrast",           "sarcasm_mod",     0.25,
                    exclusive_with=("UseRepetition",)),
    RhetoricalDevice("SelfDeprecatingHumor",  "embarrassment_mod",0.25),
    RhetoricalDevice("HeartfeltApology",      "guilt_mod",       0.20),
    
    #  харизматичные техники 
    RhetoricalDevice("Storytelling",          "charisma_mod",    0.30),
    RhetoricalDevice("InclusiveWe",           "charisma_mod",    0.25),
    RhetoricalDevice("CallToAction",          "charisma_mod",    0.20),
    
    #  убедительность / риторика 
    RhetoricalDevice("PowerStatement",        "authority_mod",   0.25),
    RhetoricalDevice("Wordplay",              "wit_mod",         0.24),
    RhetoricalDevice("StepByStep",            "patience_mod",    0.23),
    RhetoricalDevice("PersuasivePunchline",   "persuasion_mod",  0.22),
    RhetoricalDevice("RuleOfThree",           "persuasion_mod",  0.18),
    RhetoricalDevice("SlipInCurse",           "aggressiveness_mod", 0.20,
                    exclusive_with=("UseEmphaticPunctuation",)),
    
    #  декоративные спец-эффекты 
    RhetoricalDevice("InjectEmojis",          "joy_mod",         0.15),
    RhetoricalDevice("UseEmphaticPunctuation","arousal_mod",     0.10,
                    exclusive_with=("InjectAllCaps",)),
    RhetoricalDevice("UsePersonification",    "creativity_mod",  0.10),
    RhetoricalDevice("UseSymbolism",          "creativity_mod",  0.10,
                    exclusive_with=("UseMetaphors",)),
    RhetoricalDevice("UseParallelism",        "energy_mod",      0.15,
                    exclusive_with=("UseRepetition",)),
    RhetoricalDevice("UseLitotes",            "sarcasm_mod",     0.12),
    RhetoricalDevice("UseZeugma",             "creativity_mod",  0.10),
    RhetoricalDevice("UseAlliteration",       "humor_mod",       0.10,
                    exclusive_with=("UseOnomatopoeia",)),
    RhetoricalDevice("UseRepetition",         "stress_mod",      0.10,
                    exclusive_with=("UseContrast","UseParallelism")),
    RhetoricalDevice("InjectEllipses",        "anticipation_mod",0.10,
                    exclusive_with=("InjectAllCaps",)),
    
    #  мем-эффекты (почти выключены) 
    RhetoricalDevice("UseOnomatopoeia",       "energy_mod",      0.05,
                    exclusive_with=("UseAlliteration",)),
    RhetoricalDevice("InjectAllCaps",         "arousal_mod",     0.03,
                    exclusive_with=("UseEmphaticPunctuation","InjectEllipses")),
)

BOT_SIGNATURES: Set[str] = set()


def _metric(state: Dict[str, float], mods: Dict[str, float], key: str, fallback: float = 0.5) -> float:
    return mods.get(key, state.get(key.replace("_mod", ""), fallback))


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

    state: Dict[str, float] = self.state
    mods: Dict[str, float] = getattr(self, "_mods_cache", {}) or self.style_modifiers()
    weight = self._decayed_weight(uid) if uid is not None else None

    gl: List[str] = []

    top = sorted(((TONE_MAP[k], mods.get(k, state.get(k, 0.0))) for k in TONE_MAP), key=lambda t: t[1], reverse=True)[:3]
    picked = False
    for tone, val in top:
        if val >= 0.55:
            gl.append(f"Tone+={tone.name}")
            picked = True
    if not picked:
        gl.append("Tone=Neutral")

    intensity = sum(v for _, v in top) / len(top)
    gl.append("EmotionalIntensity=" + ("High" if intensity > 0.75 else "Medium" if intensity > 0.5 else "Low"))

    last_snip = self.memory_entries[-1].snippet if self.memory_entries else ""
    gl.append(f"Transition={await self.generate_transition(last_snip)}")

    energy_lvl = mods.get("energy_mod", state.get("energy_mod", state.get("energy", 0.0)))
    gl.append("Pace=" + ("Energetic" if energy_lvl > 0.75 else "Calm" if energy_lvl < 0.5 else "Moderate"))

    user_tz = getattr(self, "user_timezone", None) \
        or state.get("timezone") \
        or settings.DEFAULT_TZ
    try:
        tz = ZoneInfo(user_tz)
        hour_local = datetime.now(tz).hour
    except Exception:
        logging.warning("Invalid user_tz %r, defaulting to UTC", user_tz)
        hour_local = datetime.utcnow().hour
    
    if 0 <= hour_local < 6 and energy_lvl < 0.4:
        gl += ["Tone=Sleepy", "Pace=Unhurried", "UseShortResponses"]

    fatigue = state.get("fatigue", 0.0)
    if fatigue > 0.7:
        gl += ["Pace=Slow", "Tone=Weary", "UseShortResponses"]
    elif fatigue < 0.25 and energy_lvl > 0.6:
        self.state["flowstate_mod"] = 1.0
        gl += ["Tone=FlowState", "SentenceVariety"]

    energy_lvl = mods.get("energy_mod", state.get("energy", 0.0))
    if energy_lvl > 0.8:
        gl.append("UseShortResponses")
    elif energy_lvl < 0.2:
        gl.append("UseDetailedResponses")

    detail = "High" if (state.get("curiosity_mod", 0.0) + state.get("engagement_mod", 0.0)) / 2 > 0.75 else "Low"
    gl.append(f"LevelOfDetail={detail}")

    if self.last_user_emotions and mods.get("empathy_mod", 0.0) > 0.6:
        gl.append("EmpathyHint")

    conf = _sigmoid(state.get("confidence_mod", state.get("confidence", 0.5)))
    if conf > 0.66:
        gl.append("AnswerStyle=Assertive")
    elif conf < 0.33:
        gl.append("AnswerStyle=Tentative")

    stress = state.get("stress_mod", 0.0)
    anxiety = state.get("anxiety_mod", 0.0)
    if stress > 0.7 or anxiety > 0.7:
        gl.append("Tone=Soothing")
    elif stress < 0.3 and anxiety < 0.3 and state.get("valence_mod", 0.0) > 0.3:
        gl.append("Tone=Relaxed")

    if weight is not None:
        gl.append("AddressTone=" + ("InformalFriendly" if weight > 0.66 else "InformalNeutral" if weight > 0.33 else "InformalIndifferent"))

    if mods.get("patience_mod", 0.5) > 0.7:
        gl.append("Pace=Unhurried")

    if (state.get("joy_mod", 0.0) + mods.get("humor_mod", state.get("humor", 0.0))) / 2 > 0.5:
        gl.append("Allowance=Humor")

    if state.get("guilt_mod", 0.0) > 0.75:
        gl.append("Allowance=Apology")
    if state.get("confusion_mod", 0.0) > 0.7:
        gl.append("Allowance=Clarify")

    flirt_ok = (
        state.get("trust_mod", 0.0) > settings.THR_FLIRT_TRUST
        and state.get("valence_mod", 0.0) > -0.2
    )

    if state.get("flirtation_mod", 0.0) > settings.THR_FLIRTATION and flirt_ok:
        gl.append("Allowance=PlayfulFlirt")

    if state.get("sexual_arousal_mod", 0.0) > 0.55 and flirt_ok:
        gl.append("Allowance=SexyFlirt")

    sarcasm = state.get("sarcasm_mod", 0.0)
    aggr = state.get("aggressiveness_mod", 0.0)
    anger = state.get("anger_mod", 0.0)

    if sarcasm > 0.6 and aggr < 0.5:
        gl.append("Allowance=LightSarcasm")

    if aggr > settings.THR_CURSE_AGGR:
        gl.append("SlipInCurse")

    if anger > settings.THR_PUSHBACK_ANGER or aggr > settings.THR_PUSHBACK_AGGR:
        gl += ["ConflictStyle=PushBack", "Allowance=FirmLanguage", "Allowance=Profanity"]
    elif stress > 0.99:
        gl.append("ConflictStyle=Defuse")

    if state.get("profanity", 0.0) > settings.THR_PROFANITY and "Allowance=Profanity" not in gl:
        gl.append("Allowance=Profanity")

    gl.append(f"ProfanityLevel={state.get('profanity', 0.0):.2f}")

    PRIORITY: Tuple[str, ...] = (
        "SentenceVariety",
        "AnswerStyle=Assertive",
        "Allowance=Profanity",
        "ConflictStyle=PushBack",
        "Allowance=FirmLanguage",
        "Allowance=LightSarcasm",
        "Allowance=SexyFlirt",
        "Allowance=PlayfulFlirt",
        "Allowance=Humor",
        "InjectEmojis",
    )

    ordered_base = [p for p in PRIORITY if p in gl] + [g for g in gl if g not in PRIORITY]
    extras = _gather_extras(self, state, max_items, mods)

    core_slots = min(len(ordered_base), max_items - len(extras))
    final: List[str] = ordered_base[:core_slots] + extras

    if human_mode:
        final = [g for g in final if g.split("=")[0] not in BOT_SIGNATURES]

    if len(final) < max_items:
        pool = [item for item in ordered_base[core_slots:] + extras if item not in final and item.split("=")[0] not in BOT_SIGNATURES]
        final += pool[: max_items - len(final)]

    seen: Set[str] = set(); ordered: List[str] = []
    for itm in final:
        if itm not in seen:
            seen.add(itm)
            ordered.append(itm)

    logging.debug("Guidelines (human_mode=%s): %s", human_mode, ordered)
    return ordered
EOF