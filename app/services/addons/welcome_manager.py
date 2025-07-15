cat >app/services/addons/welcome_manager.py<< EOF
#app/services/addons/welcome_manager.py

from __future__ import annotations
import logging
import random
import asyncio

from aiogram.utils.markdown import hlink
from redis.exceptions import RedisError

from app.clients.openai_client import _call_openai_with_retry
from app.emo_engine import get_persona 
from app.config import settings
from app.core.memory import get_redis

logger = logging.getLogger(__name__)


MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0


async def can_greet(chat_id: int) -> bool:
    redis = get_redis()
    key = f"greet_times:{chat_id}"
    try:
        if await redis.set(key, 1, ex=settings.GREETING_RATE_WINDOW_SECONDS, nx=True):
            count = 1
        else:
            count = await redis.incr(key)
        ttl = await redis.ttl(key)
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

    if user.username:
        display_name = f"@{user.username}"
    else:
        display_name = user.full_name or str(user.id)
    mention = hlink(
        display_name,
        f"tg://user?id={user.id}"
    )
    
    persona = get_persona(chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("welcome_manager: persona restore timeout for chat %s", chat_id)
    except Exception:
        logger.exception("welcome_manager: error waiting for persona restore for chat %s", chat_id)

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
    ctx_len = random.choice(range(10, 30, 5))
    max_tokens = 50

    if text:
        prompt = (
            f"A new member {mention} just joined the chat and wrote: {text}. "
            f"Write a punchy, creative welcome message on your own behalf of up to {ctx_len} tokens in length."
        )
        asyncio.create_task(persona.process_interaction(user.id, text))
    else:
        prompt = (
            f"A new member {mention} just joined the chat. "
            f"Write a punchy, creative welcome message on your own behalf of up to {ctx_len} tokens in length."
        )

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
            metric_key = f"metrics:welcome:openai_timeout:{chat_id}"
            count = await redis.incr(metric_key)
            if count == 1:
                await redis.expire(metric_key, 86_400)
        except RedisError:
            logger.warning("welcome_manager: failed to record timeout metric for chat %s user %s",chat_id, user.id)
    except Exception:
        logger.exception(
            "welcome_manager: OpenAI call failed for chat %s user %s", chat_id, user.id,)
    return f"Welcome {mention}!"
EOF