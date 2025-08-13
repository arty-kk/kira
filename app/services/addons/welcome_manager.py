cat >app/services/addons/welcome_manager.py<< 'EOF'
#app/services/addons/welcome_manager.py

from __future__ import annotations
import logging
import random
import asyncio

from aiogram.utils.markdown import hlink
from redis.exceptions import RedisError

from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import get_cached_gender, push_message, get_redis
from app.clients.openai_client import _call_openai_with_retry
from app.services.responder.prompt_builder import build_system_prompt
from app.emo_engine import get_persona 
from app.config import settings

logger = logging.getLogger(__name__)


MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0
DEFAULT_MODS = {
    "creativity_mod": 0.5, "sarcasm_mod": 0.0, "enthusiasm_mod": 0.5,
    "confidence_mod": 0.5, "precision_mod": 0.5,
    "fatigue_mod":   0.0, "stress_mod":    0.0,
}

async def can_greet(chat_id: int) -> bool:
    redis = get_redis()
    key = f"greet_times:{chat_id}"
    try:
        count = await redis.incr(key)
        await redis.expire(key, settings.GREETING_RATE_WINDOW_SECONDS, nx=True)
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
    
    persona = await get_persona(chat_id)
    try:
        await persona._restored_evt.wait()
    except Exception:
        logger.exception("welcome_manager: persona restore failed")

    orig_gender = getattr(persona, "user_gender", None)

    gender = None
    async with AsyncSessionLocal() as db:
        u = await db.get(User, user.id)
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(user.id)
    if gender in ("male", "female"):
        persona.user_gender = gender
    else:
        persona.user_gender = "unknown"

    style_mods = await persona.style_modifiers() or {}
    mods = {
        k: (style_mods.get(k) if style_mods.get(k) is not None else v)
        for k, v in DEFAULT_MODS.items()
    }
    guidelines = await persona.style_guidelines(user.id)
    language = getattr(user, "language_code", None) or settings.DEFAULT_LANG

    system_msg = await build_system_prompt(persona, guidelines)
    system_msg["content"] += f"\nUser's language:{language}."

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
    ctx_len = random.choice(range(5, 25, 2))
    max_tokens = 50

    if text:
        prompt = (
            f"A new member just joined the chat and wrote: {text}. "
            f"Write him a punchy and creative welcome message on your own behalf of up to {ctx_len} tokens in length. "
            "Do NOT include user's mention in your reply."
        )
        asyncio.create_task(persona.process_interaction(user.id, text))
    else:
        prompt = (
            "A new member just joined the chat. "
            f"Write him a punchy and creative welcome message on your own behalf of up to {ctx_len} tokens in length. "
            "Do NOT include user's mention in your reply."
        )

    generated: str | None = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.RESPONSE_MODEL,
                messages=[system_msg, {"role": "user", "content": prompt}],
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
                max_completion_tokens=max_tokens,
                frequency_penalty=0.4,
                presence_penalty=0.25,
            ),
            timeout=60.0,
        )
        generated = (resp.choices[0].message.content or "").strip()
        
        if generated and generated.strip():
            generated = f"{mention} {generated.strip()}"

            try:
                await push_message(chat_id, "assistant", generated, user_id=user.id)
            except Exception:
                logger.warning("welcome_manager: push_message failed for chat %s", chat_id)
    except asyncio.TimeoutError:
        logger.warning("welcome_manager: OpenAI timeout for chat %s user %s", chat_id, user.id)
        try:
            redis = get_redis()
            metric_key = f"metrics:welcome:openai_timeout:{chat_id}"
            if await redis.incr(metric_key) == 1:
                await redis.expire(metric_key, 86_400)
        except RedisError:
            pass
    except Exception:
        logger.exception("welcome_manager: OpenAI call failed for chat %s user %s", chat_id, user.id)

    persona.user_gender = orig_gender
    return generated or f"Hello {mention}!"
EOF