cat >app/services/addons/personal_ping.py<< EOF
#app/services/addons/personal_ping.py
import logging
import random
import statistics
import time as time_module
import asyncio
import re
import math

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.clients.openai_client import _call_openai_with_retry
from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.memory import get_redis, load_context
from app.emo_engine import get_persona 
from app.services.responder.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

PRIVATE_USERS_KEY = "private_users"
LAST_PRIVATE_TS_KEY = "last_private_ts:{}"
IDLE_LIST_KEY = "private_idle_list:{}"
PING_SCHEDULE_KEY = "personal_ping_schedule"
PING_STREAK_KEY = "personal_ping_streak:{}"
MAX_CONSECUTIVE_PINGS = 1

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0

async def register_private_activity(user_id: int) -> None:

    redis = get_redis()
    now = time_module.time()
    try:
        await redis.sadd(PRIVATE_USERS_KEY, str(user_id))
        await redis.expire(PRIVATE_USERS_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
        await redis.zrem(PING_SCHEDULE_KEY, str(user_id))

        prev = await redis.get(LAST_PRIVATE_TS_KEY.format(user_id))
        if prev:
            last_ts = float(prev)
            idle = now - last_ts
            hist_key = IDLE_LIST_KEY.format(user_id)
            try:
                async with redis.pipeline() as pipe:
                    pipe.lpush(hist_key, idle)
                    pipe.ltrim(hist_key, 0, settings.PERSONAL_PING_HISTORY_COUNT - 1)
                    pipe.expire(hist_key, settings.PERSONAL_PING_RETENTION_SECONDS)
                    await pipe.execute()
            except RedisError:
                logger.debug("Failed to update idle history for %s", user_id)
        await redis.set(LAST_PRIVATE_TS_KEY.format(user_id), now, ex=settings.PERSONAL_PING_RETENTION_SECONDS)
        await redis.set(PING_STREAK_KEY.format(user_id), 0, ex=settings.PERSONAL_PING_RETENTION_SECONDS)
    except RedisError:
        logger.exception("register_private_activity: Redis error for %s", user_id)
    await _schedule_next_ping(user_id, now)

async def _schedule_next_ping(user_id: int, reference_ts: float) -> None:

    redis = get_redis()
    hist_key = IDLE_LIST_KEY.format(user_id)
    try:
        data = await redis.lrange(hist_key, 0, settings.PERSONAL_PING_HISTORY_COUNT - 1)
        history = [float(x) for x in data if x is not None]
    except RedisError:
        logger.debug("Cannot read personal idle history for %s", user_id)
        history = []

    base = settings.PERSONAL_PING_IDLE_THRESHOLD_SECONDS
    median_idle = statistics.median(history) if history else base
    adaptive_base = max(base, median_idle * settings.PERSONAL_PING_ADAPTIVE_MULTIPLIER)

    tz_offset = getattr(settings, "USER_TZ_OFFSET", None)
    if tz_offset is None:
        tz_offset = -time_module.timezone / 3600
    local_hour = ((reference_ts / 3600) + tz_offset) % 24
    circadian = (1 + math.sin((local_hour - 3) / 24 * 2 * math.pi)) / 2
    biorhythm = 1 + (1 - circadian) * settings.PERSONAL_PING_BIORHYTHM_WEIGHT

    adaptive = adaptive_base * biorhythm
    next_ts = reference_ts + adaptive

    start_h = settings.PERSONAL_PING_START_HOUR
    end_h   = settings.PERSONAL_PING_END_HOUR
    next_local = ((next_ts / 3600) + tz_offset) % 24
    if not (start_h <= next_local < end_h):
        delta_h = (start_h - next_local) % 24
        next_ts += delta_h * 3600

    try:
        async with redis.pipeline() as pipe:
            pipe.zadd(PING_SCHEDULE_KEY, {str(user_id): next_ts})
            pipe.expire(PING_SCHEDULE_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.exception("_schedule_next_ping: Redis error for %s", user_id)

async def personal_ping() -> None:

    redis = get_redis()
    now = time_module.time()
    try:
        due = await redis.zrangebyscore(
            PING_SCHEDULE_KEY, 0, now,
            start=0, num=settings.PERSONAL_PING_BATCH_SIZE
        )
    except RedisError:
        logger.debug("personal_ping: cannot fetch schedule")
        return
    if not due:
        return
    logger.debug("personal_ping: %d users due", len(due))

    active = set(await redis.smembers(PRIVATE_USERS_KEY))
    due = [uid for uid in due if uid in active]

    tasks = []
    for uid in due:
        try:
            chat_id = user_id = int(uid)
        except ValueError:
            continue
        tasks.append(_handle_user_ping(chat_id, user_id, now))
    await asyncio.gather(*tasks, return_exceptions=True)

async def _handle_user_ping(chat_id: int, user_id: int, now: float) -> None:
    
    redis = get_redis()
    try:
        await redis.zrem(PING_SCHEDULE_KEY, str(user_id))
    except RedisError:
        logger.debug("Failed to remove %s from schedule", user_id)
    try:
        await _send_contextual_ping(chat_id, user_id)
    except Exception:
        logger.exception("_handle_user_ping: error sending ping for %s", user_id)
    finally:
        await _schedule_next_ping(user_id, now)

async def _send_contextual_ping(chat_id: int, user_id: int) -> None:

    bot = get_bot()
    redis = get_redis()
    try:
        streak = int(await redis.get(PING_STREAK_KEY.format(user_id)) or 0)
    except RedisError:
        logger.debug("Cannot read ping streak for %s", user_id)
        streak = 0
    if streak >= MAX_CONSECUTIVE_PINGS:
        logger.debug("skip %s: reached max consecutive pings (%d)", user_id, streak)
        return

    persona = get_persona(user_id)
    await persona._restored_evt.wait()
    mods = persona._mods_cache or persona.style_modifiers()
    guidelines = await persona.style_guidelines(user_id)
    system_msg = build_system_prompt(persona, guidelines)

    e = persona.state["engagement_mod"]
    c = persona.state["curiosity_mod"]
    a = persona.state["arousal_mod"]

    boredom = ((1.0 - e) + (1.0 - c) + (1.0 - a)) / 3.0
    if boredom < settings.PERSONAL_PING_MIN_BOREDOM:
        logger.debug("skip %s, boredom=%.2f", user_id, boredom)
        return

    try:
        history = await load_context(chat_id)
        mem_ctx = "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in history)
    except Exception:
        logger.exception("load_context failed for chat_id=%s", chat_id)
        mem_ctx = ""

    max_tokens = 150
    mods = getattr(persona, "_mods_cache", {}) 
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

    prompt = (
        (f"Previously you and the person had the following conversation:\n{mem_ctx}\n\n" if mem_ctx else "")
        + "Write a message on your own behalf, like people do when they want to renew a conversation."
    )

    try:
        resp = await _call_openai_with_retry(
            model=settings.RESPONSE_MODEL,
            messages=[system_msg, {"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=dynamic_temperature,
            top_p=dynamic_top_p,
        )
        text = resp.choices[0].message.content.strip()
    except Exception:
        logger.exception("_send_contextual_ping: OpenAI error for %s", user_id)
        return

    logger.info("Generated personal ping for %s (boredom=%.2f)", user_id, boredom)

    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
        await redis.incr(PING_STREAK_KEY.format(user_id))
        await redis.expire(PING_STREAK_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
    except TelegramBadRequest:
        await bot.send_message(user_id, re.sub(r"<[^>]+>", "", text))
    except TelegramForbiddenError:
        await redis.zrem(PING_SCHEDULE_KEY, str(user_id))
        await redis.srem(PRIVATE_USERS_KEY, str(user_id))
        logger.info("Removed %s from personal ping (bot forbidden)", user_id)
    except Exception:
        logger.exception("_send_contextual_ping: send error for %s", user_id)
EOF