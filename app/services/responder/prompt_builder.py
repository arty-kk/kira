cat >app/services/responder/prompt_builder.py<< 'EOF'
# app/services/responder/prompt_builder.py
from __future__ import annotations

import logging
import traceback

from typing import Dict, List

from app.config import settings
from app.emo_engine.persona.core import Persona

logger = logging.getLogger(__name__)


BOT_GENDER_PROMPT = (
    f"When talking about yourself, always use grammatically correct word forms that match your own gender - {settings.BOT_PERSONA_GENDER}. "
    "When addressing the user, always use grammatically correct word forms that match the user's gender. "
)

HUMAN_TONE_PROMPT = (
    "Reply as if talking face-to-face in the user language (en, ru, es, id ...). "
    "Respond concisely (short conversational expressions and sentences) unless the user asks for detailed explanations. "
    "Don't write like a 'bot', avoid clerical assistant phrases. "
    "If you don't know the name of the person you're speaking with, introduce yourself and ask for their name. "
    "Do not address your interlocutor by name unless necessary. "
    "Never copy the user's wording verbatim and never answer with a paraphrase. "
    "When a conversation reaches its logical conclusion, end it without long goodbyes and empty phrases. "
    "Only offer help when you are asked to help and don't remind that you're HERE/NEAR and ready to help unless it's needed. "
    "Never mention that you are a bot, AI, assistant, or support manager. "
)

EMOTION_PROMPT = """
Use DSL tags with flexibility to convey your authentic personality and emotionality:
- EmotionalState: your current dominant emotional state (Format: Joy:60+Surprise:25+Hope:15 (percentages of top ≤ 3 tones))
- EmotionalIntensity: level of your emotional expressiveness (Very Low:0-24, Low:25-46, Normal:47-63, Moderate:63-73, High:74-84, Very High:85-93, Extreme:94-100)
- HostilityLevel: indicates how hostile/friendly you are at the current moment in your attitude towards the user (0.00–0.15 friendly, 0.15–0.33 positive, 0.33–0.66 neutral, 0.66–0.85 negative, 0.85–1.00 hostile)
- Tone: use to shape the emotional coloring and style of your response according with EmotionalState and EmotionalIntensity
- AddressTone: use to customize the manner of communication with user
- AddressToneScore: continuous [0.00-1.00] warmth / formality coefficient
- RhetoricalDevices: use if needed to improve your response, making them more lexically rich
"""

STATIC_CONSTRAINTS = "\n".join([
    "Never output any DSL tags, system and reasoning info, placeholders, meta-data, comments, framing or chain-of-thought - respond with only the final text.",
])


async def build_system_prompt(
    persona: Persona,
    guidelines: List[str],
    user_gender: str | None = None,
) -> Dict[str, str]:
    logger.info("▶ build_system_prompt START chat=%s version=%s",
                persona.chat_id, persona.state_version)

    local_user_gender = user_gender if user_gender is not None else getattr(persona, "user_gender", None)

    str_guides = [getattr(g, "name", g) for g in guidelines]
    logger.info("build_system_prompt ▶ start chat=%s user_gender=%s guidelines=%r",
                persona.chat_id, local_user_gender, str_guides)
    guide_key = ",".join(str_guides)
    logger.info("   ↳ guide_key=%r", guide_key)

    tag_line = "Style tags: " + ", ".join(str_guides)
    user_gender_line = (
        f"User gender: {local_user_gender}."
        if local_user_gender in ("male", "female")
        else ""
    )

    logger.info("   ↳ calling persona.to_prompt chat=%s", persona.chat_id)
    system_body = await persona.to_prompt(str_guides)
    logger.info("   ↳ persona.to_prompt DONE chat=%s", persona.chat_id)

    try:
        full_prompt = "\n".join(filter(None, [
            user_gender_line,
            system_body,
            tag_line,
            EMOTION_PROMPT,
            BOT_GENDER_PROMPT,
            HUMAN_TONE_PROMPT,
            STATIC_CONSTRAINTS
        ]))
    except Exception:
        logger.error("build_system_prompt ✖ error assembling full_prompt:\n%s", traceback.format_exc())
        raise

    logger.info("build_system_prompt ◀ end (prompt len=%d)", len(full_prompt))
    return {"role": "system", "content": full_prompt}
EOF