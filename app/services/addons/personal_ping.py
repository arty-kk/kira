cat >app/services/addons/personal_ping.py<< 'EOF'
#app/services/addons/personal_ping.py
import logging
import statistics
import time as time_module
import asyncio
import math

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from datetime import datetime, timezone, timedelta
from redis.exceptions import RedisError

from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import get_redis, load_context, push_message, get_cached_gender
from app.clients.openai_client import _call_openai_with_retry
from app.clients.telegram_client import get_bot
from app.config import settings
from app.emo_engine import get_persona 
from app.services.responder.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

PRIVATE_USERS_KEY = "private_users"
LAST_PRIVATE_TS_KEY = "last_private_ts:{}"
IDLE_LIST_KEY = "private_idle_list:{}"
PING_SCHEDULE_KEY = "personal_ping_schedule"
PING_STREAK_KEY = "personal_ping_streak:{}"
MAX_CONSECUTIVE_PINGS = 1
PERSONAL_WINDOW = 30


MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0
DEFAULT_MODS = {
    "creativity_mod": 0.5, "sarcasm_mod": 0.0, "enthusiasm_mod": 0.5,
    "confidence_mod": 0.5, "precision_mod": 0.5,
    "fatigue_mod":   0.0, "stress_mod":    0.0,
}


async def _send_private_with_retry(user_id: int, text: str) -> bool:
    bot = get_bot()
    attempt = 1
    while True:
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
            return True
        except TelegramForbiddenError:
            raise
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            logger.warning("RetryAfter %ss on PM to %s (attempt %d)", delay, user_id, attempt)
            await asyncio.sleep(delay)
            attempt += 1
        except Exception as e:
            if attempt >= 3:
                logger.exception("PM send failed for %s after %d attempts: %s", user_id, attempt, e)
                return False
            await asyncio.sleep(1.5 * attempt)
            attempt += 1


async def register_private_activity(user_id: int) -> None:

    redis = get_redis()
    now = time_module.time()
    try:
        hist_key = IDLE_LIST_KEY.format(user_id)
        last_key = LAST_PRIVATE_TS_KEY.format(user_id)
        streak_key = PING_STREAK_KEY.format(user_id)
        prev = await redis.get(last_key)
        idle = (now - float(prev)) if prev else None

        async with redis.pipeline(transaction=True) as pipe:
            pipe.sadd(PRIVATE_USERS_KEY, str(user_id))
            pipe.expire(PRIVATE_USERS_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            if idle is not None:
                pipe.lpush(hist_key, idle)
                pipe.expire(hist_key, settings.PERSONAL_PING_RETENTION_SECONDS)
            pipe.set(last_key, now, ex=settings.PERSONAL_PING_RETENTION_SECONDS)
            pipe.set(streak_key, 0, ex=settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.exception("register_private_activity: Redis error for %s", user_id)
    try:
        await _schedule_next_ping(user_id, now)
    except Exception:
        logger.exception("register_private_activity: schedule_next_ping failed for %s", user_id)


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
        try:
            tz_offset = time_module.localtime().tm_gmtoff / 3600
        except AttributeError:
            tz_offset = -getattr(time_module, "timezone", 0) / 3600
    tz = timezone(timedelta(hours=float(tz_offset)))
    _ref = datetime.fromtimestamp(reference_ts, tz)
    local_hour = _ref.hour + _ref.minute / 60.0
    circadian = (1 + math.sin((local_hour - 3) / 24 * 2 * math.pi)) / 2
    biorhythm = 1 + (1 - circadian) * settings.PERSONAL_PING_BIORHYTHM_WEIGHT

    adaptive = adaptive_base * biorhythm
    next_ts = reference_ts + adaptive

    start_h = settings.PERSONAL_PING_START_HOUR
    end_h   = settings.PERSONAL_PING_END_HOUR
    _next = datetime.fromtimestamp(next_ts, tz)
    next_local = _next.hour + _next.minute / 60.0
    if not (start_h <= next_local < end_h):
        delta_h = (start_h - next_local) % 24
        next_ts += delta_h * 3600

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            pipe.zadd(PING_SCHEDULE_KEY, {str(user_id): next_ts})
            pipe.expire(PING_SCHEDULE_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.exception("_schedule_next_ping: Redis error for %s", user_id)

async def personal_ping() -> None:

    redis = get_redis()
    now = time_module.time()
    try:
        raw = await redis.zrangebyscore(
            PING_SCHEDULE_KEY, 0, now,
            start=0, num=settings.PERSONAL_PING_BATCH_SIZE,
            withscores=True
        )
    except RedisError:
        logger.debug("personal_ping: cannot read schedule")
        return
    if not raw:
        return
    due = [
        (m.decode() if isinstance(m, (bytes, bytearray)) else str(m))
        for (m, _) in raw
    ]

    try:
        await redis.zrem(PING_SCHEDULE_KEY, *due)
    except RedisError:
        logger.debug("personal_ping: cannot remove due items")
        return
    logger.debug("personal_ping: %d users due", len(due))

    try:
        active_raw = await redis.smembers(PRIVATE_USERS_KEY)
    except RedisError:
        active_raw = set()
    active = {
        (a.decode() if isinstance(a, (bytes, bytearray)) else str(a))
        for a in active_raw
    }
    due = [uid for uid in due if uid in active]

    if not due:
        return

    tasks = []
    for uid_str in due:
        try:
            user_id = int(uid_str)
        except ValueError:
            continue
        tasks.append(
            asyncio.wait_for(
                _handle_user_ping(user_id, user_id, now),
                timeout=60
            )
        )
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def _handle_user_ping(chat_id: int, user_id: int, now: float) -> None:
    
    redis = get_redis()

    try:
        await _send_contextual_ping(chat_id, user_id)
    except Exception:
        logger.exception("_handle_user_ping: error sending ping for %s", user_id)
    finally:
        await _schedule_next_ping(user_id, now)

async def _send_contextual_ping(chat_id: int, user_id: int) -> None:

    redis = get_redis()
    try:
        streak = int(await redis.get(PING_STREAK_KEY.format(user_id)) or 0)
    except RedisError:
        logger.debug("Cannot read ping streak for %s", user_id)
        streak = 0
    if streak >= MAX_CONSECUTIVE_PINGS:
        logger.debug("skip %s: reached max consecutive pings (%d)", user_id, streak)
        return

    persona = await get_persona(chat_id)
    orig_gender = persona.user_gender

    try:
        await persona._restored_evt.wait()
    except Exception:
        logger.exception("private_ping: persona restore failed")

    gender = None
    async with AsyncSessionLocal() as db:
        u = await db.get(User, user_id)
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(user_id)

    persona.user_gender = gender if gender in ("male", "female") else "unknown"

    style_mods = await persona.style_modifiers() or {}
    mods = {
        k: (style_mods.get(k) if style_mods.get(k) is not None else v)
        for k, v in DEFAULT_MODS.items()
    }
    guidelines = await persona.style_guidelines(user_id)
    system_msg = await build_system_prompt(persona, guidelines)

    e = persona.state.get("engagement_mod", 0.5)
    c = persona.state.get("curiosity_mod", 0.5)
    a = persona.state.get("arousal_mod", 0.5)

    boredom = ((1.0 - e) + (1.0 - c) + (1.0 - a)) / 3.0
    if boredom < settings.PERSONAL_PING_MIN_BOREDOM:
        logger.debug("skip %s, boredom=%.2f", user_id, boredom)
        return

    try:
        history = await load_context(chat_id, user_id)

        summary: str | None = None
        if history and history[0].get("role") == "system":
            summary = history[0]["content"].replace("Summary:", "").strip()
            history = history[1:]

        personal_msgs = [
            m for m in history
            if m.get("user_id") == user_id or m.get("role") == "assistant"
        ][-PERSONAL_WINDOW:]

        blocks: list[str] = []
        if summary:
            blocks.append(f"Summary: {summary}")
        for m in personal_msgs:
            author = "You" if m.get("user_id") == user_id else "Me"
            blocks.append(f"{author}: {m['content']}")

        mem_ctx = "\n".join(blocks)
    except Exception:
        logger.exception("load_context failed for chat_id=%s", chat_id)
        mem_ctx = ""

    max_tokens = 150
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

    if mem_ctx:
        prompt = (
            f"Below is a conversation history with your interlocutor:\n{mem_ctx}\n"
            "____________\n"
            "The conversation was interrupted for some reason, and some time has passed.\n"
            "Try to grasp the logic during the previous conversation and, based on that, find the best motive to continue the conversation.\n"
            "Now, based on your own internal reasoning and emotional state, write your interlocutor a short message (maximum 2 sentences, up to 35 words) on your behalf that will naturally re-engage them in the conversation.\n"
            "Don't add any comments, placeholders, or internal reasoning in the final message."
        )
    else:
        prompt = (
            "It's been quiet in the private chat for a while.\n"
            "Write to the user a creative message (max 2 sentences, up to 35 words) on your own behalf to make them want to reply.\n"
            "Don't add any comments, placeholders, or internal reasoning in the final message."
        )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.RESPONSE_MODEL,
                messages=[system_msg, {"role": "user", "content": prompt}],
                max_completion_tokens=max_tokens,
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
            ),
            timeout=60.0
        )
        text = resp.choices[0].message.content.strip()
    except Exception:
        logger.exception("_send_contextual_ping: OpenAI error for %s", user_id)
        return

    logger.info("Generated personal ping for %s (boredom=%.2f)", user_id, boredom)

    try:
        ok = await _send_private_with_retry(user_id, text)
        if not ok:
            return
        try:
            await push_message(user_id, "assistant", text, user_id=user_id)
        except Exception:
            logger.exception("push_message failed for personal ping %s", user_id)
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(PING_STREAK_KEY.format(user_id))
                pipe.expire(PING_STREAK_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
                await pipe.execute()
        except RedisError:
            logger.exception("Failed to update ping streak for %s", user_id)
    except TelegramForbiddenError:
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
                pipe.srem(PRIVATE_USERS_KEY, str(user_id))
                await pipe.execute()
        except RedisError:
            logger.exception("Failed to remove forbidden user %s from ping lists", user_id)
        logger.info("Removed %s from personal ping (bot forbidden)", user_id)
    except Exception:
        logger.exception("_send_contextual_ping: error sending ping for %s", user_id)
    finally:
        persona.user_gender = orig_gender
EOF