#app/services/welcome_manager.py

from __future__ import annotations
import logging
import random
import asyncio

from aiogram.utils.markdown import hlink
from redis.exceptions import RedisError

from app.clients.openai_client import _call_openai_with_retry
from app.emo_engine.registry import get_persona 
from app.config import settings
from app.core import get_redis

logger = logging.getLogger(__name__)


MAX_TEMPERATURE = 0.85
MIN_TEMPERATURE = 0.5
TOP_P_MIN = 0.7
TOP_P_MAX = 1.0


async def can_greet(chat_id: int) -> bool:
    redis = get_redis()
    key = f"greet_times:{chat_id}"
    try:
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = await pipe.execute()
        if ttl < 0:
            await redis.expire(key, settings.GREETING_RATE_WINDOW_SECONDS)
    except RedisError:
        logger.warning("can_greet: Redis error, allowing greeting for chat %s", chat_id)
        return True
    allowed = count <= settings.GREETING_RATE_LIMIT
    logger.debug(
        "can_greet: key=%s count=%d window=%ds limit=%d allowed=%s",
        key,
        count,
        settings.GREETING_RATE_WINDOW_SECONDS,
        settings.GREETING_RATE_LIMIT,
        allowed,
    )
    return allowed

async def generate_welcome(chat_id: int, user, text: str) -> str:

    display_name = user.username or (user.full_name or str(user.id))
    mention_link = hlink(display_name, f"tg://user?id={user.id}")
    PLACEHOLDER = "{{MENTION}}"
    
    persona = get_persona(chat_id)
    try:
        await persona._restored_evt.wait()
    except Exception:
        pass

    try:
        mods = persona.style_modifiers()
    except Exception:
        logger.exception("welcome_manager: failed to compute style_modifiers for chat %s", chat_id)
        mods = {
            "creativity_mod": 1.0, "sarcasm_mod": 0.0, "enthusiasm_mod": 1.0,
            "confidence_mod": 1.0, "precision_mod": 1.0, "fatigue_mod": 0.0, "stress_mod": 0.0,
        }
    persona._mods_cache = mods
    guidelines = await persona.style_guidelines(user.id)

    mods = persona._mods_cache
    language = getattr(user, "language_code", None) or settings.DEFAULT_LANG

    system_msg = {
        "role": "system",
        "content": (
            persona.to_prompt(guidelines)
            + f"\nReply in the user's language ({language})."
        ),
    }

    novelty = (
        0.4 * mods["creativity_mod"]
      + 0.4 * mods["sarcasm_mod"]
      + 0.2 * mods["enthusiasm_mod"]
    )
    coherence = (
        0.5 * mods["confidence_mod"]
      + 0.3 * mods["precision_mod"]
      + 0.1 * (1 - mods["fatigue_mod"])
      + 0.1 * (1 - mods["stress_mod"])
    )
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_temperature = min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, dynamic_temperature))
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    dynamic_top_p = min(TOP_P_MAX, max(TOP_P_MIN, dynamic_top_p))

    max_tokens = 50

    if text:
        prompt = (
            f"A new member {PLACEHOLDER} just joined the chat and wrote: {text}. "
            f"Write a single-line, creative welcome message on your own behalf. "
            f"Respond with only the final text, no explanations, comments, or framing."
        )
        async def _safe_interaction():
            try:
                await persona.process_interaction(user.id, text)
            except Exception:
                logger.exception("welcome_manager: process_interaction failed for user %s", user.id)
        asyncio.create_task(_safe_interaction())
    else:
        prompt = (
            f"A new member {PLACEHOLDER} just joined the chat. "
            f"Write a single-line, creative welcome message on your own behalf. "
            f"Respond with only the final text, no explanations, comments, or framing."
        )

    raw: str = f"Welcome {mention_link}!"
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.RESPONSE_MODEL,
                messages=[system_msg, {"role": "user", "content": prompt}],
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
                max_tokens=max_tokens,
            ),
            timeout=30.0,
        )
        generated = resp.choices[0].message.content.strip()
        if generated:
            return generated

    except asyncio.TimeoutError:
        logger.warning("welcome_manager: OpenAI timeout for chat %s user %s", chat_id, user.id)
        try:
            redis = get_redis()
            count = await redis.incr("metrics:welcome:openai_timeout")
            if count == 1:
                await redis.expire("metrics:welcome:openai_timeout", 86_400)
        except RedisError:
            logger.warning("welcome_manager: failed to record timeout metric for chat %s", chat_id)

    except Exception:
        logger.exception(
            "welcome_manager: OpenAI call failed for chat %s user %s",
            chat_id,
            user.id,
        )

    return raw