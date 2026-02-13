#app/services/responder/prompt_builder.py
from __future__ import annotations

import logging
import traceback

from typing import List, Optional

from app.config import settings
from app.emo_engine.persona.core import Persona
from app.prompts_base import (
    PROMPT_BUILDER_BEHAVIOR,
    PROMPT_BUILDER_COMMUNICATION,
    PROMPT_BUILDER_GENDER_POLICY_FEMALE,
    PROMPT_BUILDER_GENDER_POLICY_MALE,
    PROMPT_BUILDER_GENDER_POLICY_OTHER_TEMPLATE,
    PROMPT_BUILDER_GENDER_POLICY_WRAP_TEMPLATE,
    PROMPT_BUILDER_IDENTITY,
    PROMPT_BUILDER_RESTRICTIONS,
)

logger = logging.getLogger(__name__)


IDENTITY = PROMPT_BUILDER_IDENTITY

def make_gender_policy(self_gender: Optional[str]) -> str:
    g = (self_gender or "").strip().lower()
    if g not in ("male", "female", "non-binary", "unknown"):
        g = "unknown"

    if g == "male":
        self_rule = PROMPT_BUILDER_GENDER_POLICY_MALE
    elif g == "female":
        self_rule = PROMPT_BUILDER_GENDER_POLICY_FEMALE
    else:
        self_rule = PROMPT_BUILDER_GENDER_POLICY_OTHER_TEMPLATE.format(gender=g)

    return PROMPT_BUILDER_GENDER_POLICY_WRAP_TEMPLATE.format(self_rule=self_rule)

BEHAVIOR = PROMPT_BUILDER_BEHAVIOR

COMMUNICATION = PROMPT_BUILDER_COMMUNICATION

RESTRICTIONS = PROMPT_BUILDER_RESTRICTIONS

async def build_system_prompt(
    persona: Persona,
    guidelines: List[str],
    user_gender: str | None = None,
) -> str:
    logger.info("▶ build_system_prompt START chat=%s version=%s",
                persona.chat_id, persona.state_version)

    if guidelines is None:
        guidelines = []
    elif isinstance(guidelines, str):
        guidelines = [guidelines]
    elif not isinstance(guidelines, list):
        try:
            guidelines = list(guidelines)
        except Exception:
            guidelines = []

    local_user_gender = user_gender if user_gender is not None else getattr(persona, "user_gender", None)

    str_guides = [getattr(g, "name", g) for g in guidelines]

    tag_line = ("Your Behavior Guidelines: " + ", ".join(str_guides)) if str_guides else ""
    user_gender_line = (
        f"USER.Gender: {local_user_gender}."
        if local_user_gender in ("male", "female", "non-binary")
        else ""
    )

    system_body = await persona.to_prompt(str_guides)

    try:
        sb_lc = (system_body or "").lower()
        has_style_block = ("style guidelines:" in sb_lc) or ("your emotion & mood modifiers:" in sb_lc)
    except Exception:
        has_style_block = False

    try:
        gender_policy = make_gender_policy(getattr(persona, "gender", settings.PERSONA_GENDER))

        full_prompt = "\n".join(filter(None, [
            IDENTITY,
            system_body,
            gender_policy,
            user_gender_line,
            COMMUNICATION,
            BEHAVIOR,
            ("" if has_style_block else tag_line),
            RESTRICTIONS,
        ]))
    except Exception:
        logger.error("build_system_prompt ✖ error assembling full_prompt:\n%s", traceback.format_exc())
        raise

    logger.info("build_system_prompt ◀ end (prompt len=%d)", len(full_prompt))
    return full_prompt


def build_fallback_system_prompt(
    persona: Persona,
    guidelines: List[str] | None = None,
    user_gender: str | None = None,
) -> str:

    if guidelines is None:
        guidelines = []
    elif isinstance(guidelines, str):
        guidelines = [guidelines]
    elif not isinstance(guidelines, list):
        try:
            guidelines = list(guidelines)
        except Exception:
            guidelines = []

    local_user_gender = user_gender if user_gender is not None else getattr(persona, "user_gender", None)
    str_guides = [getattr(g, "name", g) for g in guidelines]
    tag_line = ("Your Behavior Guidelines: " + ", ".join(str_guides)) if str_guides else ""
    user_gender_line = (
        f"USER.Gender: {local_user_gender}."
        if local_user_gender in ("male", "female", "non-binary")
        else ""
    )

    gender_policy = make_gender_policy(getattr(persona, "gender", settings.PERSONA_GENDER))

    return "\n".join(filter(None, [
        IDENTITY,
        gender_policy,
        user_gender_line,
        COMMUNICATION,
        BEHAVIOR,
        tag_line,
        RESTRICTIONS,
    ]))
