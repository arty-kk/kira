#app/services/addons/welcome_manager.py
from __future__ import annotations

import logging
import random
import asyncio
import time

from typing import Optional
from aiogram.utils.markdown import hlink
from redis.exceptions import RedisError

from app.core.db import session_scope
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
    "curiosity_mod": 0.5,
    "technical_mod": 0.0,
    "valence_mod": 0.0,
}

def _merge_and_clamp_mods(style_mods: dict | None) -> dict:
    mods = DEFAULT_MODS.copy()
    if not isinstance(style_mods, dict):
        return mods
    for k in mods.keys():
        try:
            if k == "valence_mod":
                x = float(style_mods.get("valence_mod", style_mods.get("valence", mods[k])))
                mods[k] = max(-1.0, min(1.0, x))
            else:
                x = float(style_mods.get(k, mods[k]))
                mods[k] = max(0.0, min(1.0, x))
        except Exception:
            pass
    return mods

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

    stored_lang = None
    try:
        redis = get_redis()
        raw = await redis.get(f"lang:{user.id}")
        stored_lang = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    except Exception:
        logger.debug("welcome_manager: failed to read preferred language from redis for user %s", user.id, exc_info=True)

    lang_code = (stored_lang or getattr(user, "language_code", None) or settings.DEFAULT_LANG or "en")
    lang_code = str(lang_code).split("-", 1)[0].lower()

    gender = None
    async with session_scope(stmt_timeout_ms=2000, read_only=True) as db:
        u = await db.get(User, user.id)
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(user.id)
    user_gender_val = gender if gender in ("male", "female") else None

    style_mods = await persona.style_modifiers() or {}
    mods = _merge_and_clamp_mods(style_mods)
    guidelines = await persona.style_guidelines(user.id)

    system_msg = await build_system_prompt(persona, guidelines, user_gender=user_gender_val)
    system_msg = f"{system_msg}\nReply ONLY in the user's language: {lang_code}. Do not switch languages."

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
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    max_tokens = 120

    try:
        dynamic_temperature *= (1.0 + 0.10 * float(mods["valence_mod"]))
    except Exception:
        pass

    if dynamic_temperature < 0.55: dynamic_temperature = 0.55
    if dynamic_temperature > 0.70: dynamic_temperature = 0.70
    if dynamic_top_p < 0.85: dynamic_top_p = 0.85
    if dynamic_top_p > 0.98: dynamic_top_p = 0.98

    try:
        logger.info(
            "WELCOME PM TRACE chat=%s user=%s lang=%s novelty=%.3f coherence=%.3f temp=%.2f top_p=%.2f "
            "mods[c/sa/e/conf/prec/fat/str/val]=[%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f]",
            chat_id, user.id, lang_code, novelty, coherence, dynamic_temperature, dynamic_top_p,
            mods.get("creativity_mod",0.0), mods.get("sarcasm_mod",0.0), mods.get("enthusiasm_mod",0.0),
            mods.get("confidence_mod",0.0), mods.get("precision_mod",0.0), mods.get("fatigue_mod",0.0),
            mods.get("stress_mod",0.0), mods.get("valence_mod",0.0)
        )
    except Exception:
        pass

    if text:
        prompt = (
            f"A new member just joined the chat and wrote:\n{text}.\n\n"
            f"Write a short and creative welcome on your behalf. "
            f"Use language '{lang_code}' only (1–2 sentences). "
            "Do NOT include the user's mention in your reply."
        )
        asyncio.create_task(persona.process_interaction(user.id, text))
    else:
        prompt = (
            "A new member just joined the chat.\n"
            f"Write a short and creative welcome on your behalf. "
            f"Use language '{lang_code}' only (1–2 sentences). "
            "Do NOT include the user's mention in your reply."
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
            timeout=settings.RESPONSE_MODEL_TIMEOUT,
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
        
    fallback_map = {
        "ru": "Добро пожаловать! Рады видеть тебя 😊",
        "en": "Welcome! Glad to have you here 😊",
        "es": "¡Bienvenido! Nos alegra que estés aquí 😊",
        "pt": "Bem-vindo! Que bom ter você aqui 😊",
        "de": "Willkommen! Schön, dass du da bist 😊",
        "fr": "Bienvenue ! Ravi de t’avoir ici 😊",
        "tr": "Hoş geldin! Seni burada görmek güzel 😊",
        "ar": "أهلًا وسهلًا! سعداء بوجودك 😊",
        "id": "Selamat datang! Senang kamu di sini 😊",
        "vi": "Chào mừng! Rất vui vì bạn có mặt 😊",
        "uk": "Ласкаво просимо! Радий(а) тебе бачити 😊",
        "kk": "Қош келдің! Сені көргенімізге қуаныштымыз 😊",
    }
    localized_fallback = f"{mention} {fallback_map.get(lang_code, fallback_map['en'])}"
    return (generated or localized_fallback).strip()


async def generate_private_welcome(chat_id: int, user: Optional[object]) -> str:

    uid = getattr(user, "id", chat_id)
    
    persona = await get_persona(chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("welcome_manager: persona restore failed (private)")

    gender = None
    async with session_scope(stmt_timeout_ms=2000, read_only=True) as db:
        u = await db.get(User, uid)
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(uid)
    user_gender_val = gender if gender in ("male", "female") else None

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(persona.style_modifiers(), 30)
    except Exception:
        logger.exception("style_modifiers acquisition failed")
        style_mods = {}
    mods = _merge_and_clamp_mods(style_mods)
    guidelines = await persona.style_guidelines(uid)

    stored_lang = None
    try:
        redis = get_redis()
        raw = await redis.get(f"lang:{uid}")
        if isinstance(raw, (bytes, bytearray)):
            stored_lang = raw.decode()
        else:
            stored_lang = raw
    except Exception:
        logger.debug("welcome_manager: failed to read preferred language from redis for user %s", uid, exc_info=True)

    lang_code = (stored_lang or getattr(user, "language_code", None) or settings.DEFAULT_LANG or "en")
    lang_code = lang_code.split("-", 1)[0].lower()

    system_msg = await build_system_prompt(persona, guidelines, user_gender=user_gender_val)
    system_msg = f"{system_msg}\nUser's language:{lang_code}."

    novelty = 0.4 * mods["creativity_mod"] + 0.4 * mods["sarcasm_mod"] + 0.2 * mods["enthusiasm_mod"]
    coherence = 0.5 * mods["confidence_mod"] + 0.3 * mods["precision_mod"] + 0.1 * (1 - mods["fatigue_mod"]) + 0.1 * (1 - mods["stress_mod"])
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    try:
        dynamic_temperature *= (1.0 + 0.10 * float(mods["valence_mod"]))
    except Exception:
        pass
    if dynamic_temperature < 0.55: dynamic_temperature = 0.55
    if dynamic_temperature > 0.70: dynamic_temperature = 0.70
    if dynamic_top_p < 0.85: dynamic_top_p = 0.85
    if dynamic_top_p > 0.98: dynamic_top_p = 0.98
    max_tokens = 250

    try:
        logger.info(
            "WELCOME TRACE chat=%s user=%s lang=%s novelty=%.3f coherence=%.3f temp=%.2f top_p=%.2f "
            "mods[c/sa/e/conf/prec/fat/str/val]=[%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f]",
            chat_id, uid, lang_code, novelty, coherence, dynamic_temperature, dynamic_top_p,
            mods.get("creativity_mod",0.0), mods.get("sarcasm_mod",0.0), mods.get("enthusiasm_mod",0.0),
            mods.get("confidence_mod",0.0), mods.get("precision_mod",0.0), mods.get("fatigue_mod",0.0),
            mods.get("stress_mod",0.0), mods.get("valence_mod",0.0))
    except Exception:
        pass

    prompt = (
        "A user just started a private chat with you.\n"
        f"The user's language code is {lang_code}. Use this language to respond to the user.\n"
        "Greet him short and punchy on your own behalf."
        #"Tell them that you can be their personal conseillerie, psychologist, coach, friend, or just a pleasant conversational partner, and that your abilities are limited only by their imagination."
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
            timeout=settings.RESPONSE_MODEL_TIMEOUT,
        )
        generated = (_get_output_text(resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("welcome_manager: OpenAI timeout (private) for chat %s user %s", chat_id, uid)
    except Exception:
        logger.exception("welcome_manager: OpenAI call failed (private) for chat %s user %s", chat_id, uid)
    finally:
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
        await push_message(chat_id, "assistant", final_text, user_id=uid)
    except Exception:
        logger.warning("welcome_manager: push_message failed for private chat %s", chat_id)

    try:
        redis = get_redis()
        key = f"last_ping:pm:{uid}"
        await redis.hset(key, mapping={"ts": int(time.time()), "text": final_text})
        await redis.expire(key, int(getattr(settings, "PERSONAL_PING_RETENTION_SECONDS", 86_400)))
    except Exception:
        logger.debug("welcome_manager: failed to set last_ping for pm", exc_info=True)

    return final_text