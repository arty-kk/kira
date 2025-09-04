#app/services/addons/welcome_manager.py

from __future__ import annotations
import logging
import random
import asyncio
import time

from typing import Optional
from aiogram.utils.markdown import hlink
from redis.exceptions import RedisError

from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import get_cached_gender, push_message, get_redis
from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.services.responder.prompt_builder import build_system_prompt
from app.emo_engine import get_persona 
from app.config import settings

logger = logging.getLogger(__name__)


MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0

DEFAULT_MODS = {
    "creativity_mod": 0.5,
    "sarcasm_mod": 0.0,
    "enthusiasm_mod": 0.5,
    "confidence_mod": 0.5,
    "precision_mod": 0.5,
    "fatigue_mod": 0.0,
    "stress_mod": 0.0,
}


async def can_greet(chat_id: int) -> bool:
    redis = get_redis()
    key = f"greet_times:{chat_id}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, settings.GREETING_RATE_WINDOW_SECONDS, nx=True)
            res = await pipe.execute()
        count = int(res[0] or 0)
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
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("welcome_manager: persona restore failed")

    orig_gender: Optional[str] = getattr(persona, "user_gender", None)

    gender = None
    async with AsyncSessionLocal() as db:
        u = await db.get(User, user.id)
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(user.id)

    persona.user_gender = gender if gender in ("male", "female") else "unknown"

    style_mods = await persona.style_modifiers() or {}
    mods = {
        k: (style_mods.get(k) if style_mods.get(k) is not None else v)
        for k, v in DEFAULT_MODS.items()
    }
    guidelines = await persona.style_guidelines(user.id)
    language = getattr(user, "language_code", None) or settings.DEFAULT_LANG

    system_msg = await build_system_prompt(persona, guidelines)
    system_msg = f"{system_msg}\nUser's language:{language}."

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
    max_tokens = 120

    if text:
        prompt = (
            f"A new member just joined the chat and wrote:\n{text}.\n\n"
            f"Write him a short, punchy and creative welcome message on your own behalf.\n"
            "Also, add to your greeting that you can answer any questions about the GalaxyTap game."
            "Do NOT include user's mention in your reply."
        )
        asyncio.create_task(persona.process_interaction(user.id, text))
    else:
        prompt = (
            "A new member just joined the chat.\n"
            f"Write him a short, punchy and creative welcome message on your own behalf.\n"
            "Also, add to your greeting that you can answer any questions about the GalaxyTap game."
            "Do NOT include user's mention in your reply."
        )

    generated: Optional[str] = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.RESPONSE_MODEL,
                input=[
                    _msg("system", system_msg),
                    _msg("user", prompt)
                ],
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
                max_output_tokens=max_tokens,
            ),
            timeout=60.0,
        )
        generated = (_get_output_text(resp) or "").strip()
        
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
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(metric_key)
                pipe.expire(metric_key, 86_400, nx=True)
                await pipe.execute()
        except RedisError:
            pass
    except Exception:
        logger.exception("welcome_manager: OpenAI call failed for chat %s user %s", chat_id, user.id)

    try:
        persona.user_gender = orig_gender
    except Exception:
        pass
        
    return generated or f"Hello {mention}!"


async def generate_private_welcome(chat_id: int, user) -> str:

    persona = await get_persona(chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("welcome_manager: persona restore failed (private)")

    orig_gender = getattr(persona, "user_gender", None)
    gender = None
    async with AsyncSessionLocal() as db:
        u = await db.get(User, user.id)
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(user.id)
    persona.user_gender = gender if gender in ("male", "female") else "unknown"

    style_mods = await persona.style_modifiers() or {}
    mods = {k: (style_mods.get(k) if style_mods.get(k) is not None else v) for k, v in DEFAULT_MODS.items()}
    guidelines = await persona.style_guidelines(user.id)

    stored_lang = None
    try:
        redis = get_redis()
        raw = await redis.get(f"lang:{user.id}")
        if isinstance(raw, (bytes, bytearray)):
            stored_lang = raw.decode()
        else:
            stored_lang = raw
    except Exception:
        logger.debug("welcome_manager: failed to read preferred language from redis for user %s", user.id, exc_info=True)

    lang_code = (stored_lang or getattr(user, "language_code", None) or settings.DEFAULT_LANG or "en")
    lang_code = lang_code.split("-", 1)[0].lower()

    system_msg = await build_system_prompt(persona, guidelines)
    system_msg = f"{system_msg}\nUser's language:{lang_code}."

    novelty = 0.4 * mods["creativity_mod"] + 0.4 * mods["sarcasm_mod"] + 0.2 * mods["enthusiasm_mod"]
    coherence = 0.5 * mods["confidence_mod"] + 0.3 * mods["precision_mod"] + 0.1 * (1 - mods["fatigue_mod"]) + 0.1 * (1 - mods["stress_mod"])
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_temperature = min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, dynamic_temperature))
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    dynamic_top_p = min(TOP_P_MAX, max(TOP_P_MIN, dynamic_top_p))
    max_tokens = 120

    prompt = (
        "A user just started a private chat with you.\n"
        f"The user's language code that they speak is {lang_code}. Use this language to respond to the user.\n"
        "Greet him on your own behalf with a friendly short message and ask how he is doing."
    )

    generated = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.RESPONSE_MODEL,
                input=[
                    _msg("system", system_msg),
                    _msg("user", prompt)
                ],
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
                max_output_tokens=max_tokens,
            ),
            timeout=60.0,
        )
        generated = (_get_output_text(resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("welcome_manager: OpenAI timeout (private) for chat %s user %s", chat_id, user.id)
    except Exception:
        logger.exception("welcome_manager: OpenAI call failed (private) for chat %s user %s", chat_id, user.id)
    finally:
        try:
            persona.user_gender = orig_gender
        except Exception:
            pass

    fallback = {
        "ru": "Привет! Как дела? 😊",
        "en": "Hi! How are you? 😊",
        "es": "¡Hola! ¿Cómo estás? 😊",
        "pt": "Olá! Como vai? 😊",
        "de": "Hallo! Wie geht es dir? 😊",
        "fr": "Salut ! Comment vas-tu ? 😊",
        "tr": "Merhaba! Nasılsınız? 😊",
        "ar": "مرحباً! كيف حالك؟ 😊",
        "id": "Hai! Apa kabar? 😊",
        "vi": "Xin chào! Bạn khỏe không? 😊",
    }.get(lang_code, "Hi!")

    final_text = (generated or fallback).strip()


    try:
        await push_message(chat_id, "assistant", final_text, user_id=user.id)
    except Exception:
        logger.warning("welcome_manager: push_message failed for private chat %s", chat_id)

    try:
        redis = get_redis()
        key = f"last_ping:pm:{user.id}"
        await redis.hset(key, mapping={"ts": int(time.time()), "text": final_text})
        await redis.expire(key, int(getattr(settings, "PERSONAL_PING_RETENTION_SECONDS", 86_400)))
    except Exception:
        logger.debug("welcome_manager: failed to set last_ping for pm", exc_info=True)

    return final_text
