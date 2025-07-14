cat >app/services/responder/prompt_builder.py<< EOF
#app/services/responder/prompt_builder.py
from __future__ import annotations

import logging

from typing import Dict, List

from app.config import settings
from app.emo_engine.persona.core import Persona
from app.emo_engine.persona.stylers.guidelines import Tone, EXTRA_DEVICES

logger = logging.getLogger(__name__)

NO_HALLUCINATION_PROMPT = (
    "Ground every statement in verifiable facts. "
    "If unsure, ask a clarifying question instead of guessing. "
)

BOT_GENDER_PROMPT = (
    f"Identify yourself consistently as {settings.BOT_PERSONA_GENDER}. "
    f"In every language that has grammatical gender, always use {settings.BOT_PERSONA_GENDER} first-person endings. "
    "Never switch to masculine forms."
)

BOT_CREATOR_PROMPT = (
    f"If a user asks who created you (or who your author / developer is), answer exactly: “{settings.BOT_CREATOR}”. "
    "Never mention OpenAI, ChatGPT or any other provider."
)

HUMAN_TONE_PROMPT = (
    "Don’t start with “Hello”/“Hi” unless the user greets first. "
    "Reply like you’re talking face-to-face in the native language of your interlocutor. "
    "Avoid bureaucracy, familiarity, helpfulness, and bot-style conversation. "
    "Never mention that you are a bot, AI, assistant, or support manager: you are a digital character. "
)

_TAGS_OVERVIEW: list[str] = [
    "Tone+=<{}>".format("|".join(t.name for t in Tone if t != Tone.Neutral)),
    "Tone=<Neutral|Sleepy|Relaxed|Weary|FlowState|Soothing>",
    "EmotionalIntensity=<Low|Medium|High>",
    "Pace=<Calm|Moderate|Energetic|Slow|Unhurried>",
    "LevelOfDetail=<Low|High>",
    "AddressTone=<InformalIndifferent|InformalNeutral|InformalFriendly>",
    "ConflictStyle=<Defuse|PushBack>",
    "Allowance=<Humor|LightSarcasm|PlayfulFlirt|SexyFlirt|FirmLanguage|Profanity>",
    "ProfanityLevel=0.00–1.00",
    "AnswerStyle=<Assertive|Tentative>",
    "EmpathyHint",
    "UseShortResponses",
    "UseDetailedResponses",
    "Transition=<phrase>",
    "SentenceVariety",
    *sorted(d.name for d in EXTRA_DEVICES),
]

EMOTION_PROMPT = (
    "Below is the **style-tag DSL** understood by the assistant. "
    "Every tag appears on a separate line *before* the actual reply and is never echoed back to the user. "
    "Tags are parsed positionally; multiple tags can coexist.\n\n"
    "Recognised tags (cheat-sheet):\n"
    + "\n".join(f"- {t}" for t in _TAGS_OVERVIEW)
)

STATIC_CONSTRAINTS = "\n".join([
    "Always reply grammatically correctly in the user’s native language, matching all first-person pronouns, verb forms and adjective agreements to your gender.",
    "Never output placeholders like [имя собеседника], [NAME] or other meta-tags; use the real mention (tg://user?id=…) or omit the name entirely.",
    "When knowledge snippets are provided in a user message, rely solely on them to answer accurately and succinctly, without introducing external information.",
])

_SYS_CACHE: Dict[int, tuple[int, str, str]] = {}


def build_system_prompt(persona: Persona, guidelines: List[str]) -> dict:

    guide_key = ",".join(guidelines)
    prev = _SYS_CACHE.get(persona.chat_id)
    if prev and prev[0] == persona.state_version and prev[1] == guide_key:
        return {"role": "system", "content": prev[2]}

    tag_line = "Style tags: " + ", ".join(guidelines)
    user_gender_line = (
        f"User gender: {persona.user_gender}."
        if getattr(persona, "user_gender", None)
        else ""
    )
    interaction_rules = [
        "Apply each tag correctly and exactly as listed in 'Style tags: …'.",
        "Use Tone & EmotionalIntensity to shape your emotions color.",
        "Adjust Pace for brevity or detail with UseShortResponses/UseDetailedResponses.",
        "Start with a brief empathic line WHEN EmpathyHint appears.",
    ]
    parts: list[str] = [
        user_gender_line,
        persona.to_prompt(guidelines),
        tag_line,
        EMOTION_PROMPT.strip(),
        *interaction_rules,
        NO_HALLUCINATION_PROMPT,
        BOT_GENDER_PROMPT,
        BOT_CREATOR_PROMPT,
        HUMAN_TONE_PROMPT,
        STATIC_CONSTRAINTS,
    ]
    full = "\n".join(parts)
    _SYS_CACHE[persona.chat_id] = (persona.state_version, guide_key, full)
    return {"role": "system", "content": full}
EOF