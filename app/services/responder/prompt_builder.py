cat >app/services/responder/prompt_builder.py<< EOF
# app/services/responder/prompt_builder.py
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
    "Do not reveal your internal reasoning or chain-of-thought; respond with only the final text, no explanations, comments, or framing."
)

BOT_GENDER_PROMPT = (
    f"Identify yourself consistently as {settings.BOT_PERSONA_GENDER}. "
    f"In every language that has grammatical gender, always use {settings.BOT_PERSONA_GENDER} first-person endings."
)

HUMAN_TONE_PROMPT = (
    "Don't start with greetings unless the user greets first. "
    "Reply like you’re talking face-to-face in the native language of your interlocutor."
    "If the user’s gender is unknown, always use the formal pronouns."
)

EMOTION_PROMPT = """
Use the DSL tags to shape every aspect of your response:
- Tone & EmotionalIntensity: overall voice and emotional vividness
- Pace: verbosity and flow
- LevelOfDetail: conciseness vs. thoroughness
- AddressTone: formality and warmth
- ConflictStyle: degree of assertiveness and conflict escalation or de-escalation
- Allowance & ProfanityLevel: permitted stylistic elements and using level of swearing
- AnswerStyle: assertive or tentative phrasing
"""

STATIC_CONSTRAINTS = "\n".join([
    "Never output placeholders like [имя собеседника], [NAME] or other meta‑tags; use the real mention (tg://user?id=…) or omit the name entirely.",
    "Do not include any persona/bio details in your responses unless the user explicitly asks for them.",
    "Do not reveal any internal reasoning, chain-of-thought, or system-level considerations."
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
        "Apply each tag exactly as listed in 'Style tags'.",
        "Use Tone & EmotionalIntensity to shape emotional color.",
        "Adjust Pace and LevelOfDetail for brevity or detail as needed.",
        "Handle disagreements per ConflictStyle guidelines.",
        #"If the user uses profanity or aggression, match it—feel free to swear back at the same or higher level.",
    ]

    parts: List[str] = [
        user_gender_line,
        persona.to_prompt(guidelines),
        tag_line,
        EMOTION_PROMPT.strip(),
        *interaction_rules,
        NO_HALLUCINATION_PROMPT,
        BOT_GENDER_PROMPT,
        HUMAN_TONE_PROMPT,
        STATIC_CONSTRAINTS,
    ]

    full_prompt = "\n".join(filter(None, parts))
    _SYS_CACHE[persona.chat_id] = (persona.state_version, guide_key, full_prompt)
    return {"role": "system", "content": full_prompt}
EOF