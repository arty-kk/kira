cat >app/services/addons/personal_ping.py<< 'EOF'
#app/services/addons/personal_ping.py
import logging
import statistics
import time as time_module
import asyncio
import math
import random

from typing import Iterable
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from datetime import datetime
from redis.exceptions import RedisError
from zoneinfo import ZoneInfo

from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import get_redis, load_context, push_message, get_cached_gender
from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.clients.telegram_client import get_bot
from app.config import settings
from app.emo_engine import get_persona
from app.services.responder.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

LAST_PRIVATE_TS_KEY = "last_private_ts:{}"
IDLE_LIST_KEY = "private_idle_list:{}"
PING_SCHEDULE_KEY = "personal_ping_schedule"
PING_STREAK_KEY = "personal_ping_streak:{}"

MAX_CONSECUTIVE_PINGS = getattr(settings, "PERSONAL_PING_MAX_CONSECUTIVE", 3)
PERSONAL_WINDOW = 30

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

_CLAIM_DUE_SHA: str | None = None
_BACKOFF_MULT = getattr(settings, "PERSONAL_PING_BACKOFF_MULT", 3.0)
_BACKOFF_MAX_HOURS = getattr(settings, "PERSONAL_PING_BACKOFF_MAX_HOURS", 48)
_BACKOFF_JITTER_PCT = getattr(settings, "PERSONAL_PING_BACKOFF_JITTER_PCT", 0.10)


async def _send_private_with_retry(user_id: int, text: str) -> int | None:
    bot = get_bot()
    attempt = 1
    while True:
        try:
            await asyncio.sleep(random.uniform(0.0, 0.2))
            msg = await bot.send_message(user_id, text, parse_mode=None)
            return int(msg.message_id)
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
                return None
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
        if prev is not None:
            try:
                prev_ts = float(prev.decode() if isinstance(prev, (bytes, bytearray)) else prev)
                idle = now - prev_ts
            except Exception:
                idle = None
        else:
            idle = None

        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            if idle is not None:
                pipe.lpush(hist_key, idle)
                pipe.ltrim(hist_key, 0, settings.PERSONAL_PING_HISTORY_COUNT - 1)
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
        history = []
        for x in (data or []):
            try:
                history.append(float(x.decode() if isinstance(x, (bytes, bytearray)) else x))
            except Exception:
                continue
    except RedisError:
        logger.debug("Cannot read personal idle history for %s", user_id)
        history = []

    base = settings.PERSONAL_PING_IDLE_THRESHOLD_SECONDS
    median_idle = statistics.median(history) if history else base
    adaptive_base = max(base, median_idle * settings.PERSONAL_PING_ADAPTIVE_MULTIPLIER)

    tz = await _user_zoneinfo(user_id)
    _ref = datetime.fromtimestamp(reference_ts, tz)
    local_hour = _ref.hour + _ref.minute / 60.0
    circadian = (1 + math.sin((local_hour - 3) / 24 * 2 * math.pi)) / 2
    biorhythm = 1 + (1 - circadian) * settings.PERSONAL_PING_BIORHYTHM_WEIGHT

    try:
        raw_streak = await redis.get(PING_STREAK_KEY.format(user_id))
        streak = int((raw_streak.decode() if isinstance(raw_streak, (bytes, bytearray)) else raw_streak) or 0)
        if streak < 0: streak = 0
    except Exception:
        streak = 0

    base_interval = adaptive_base * biorhythm
    backoff_factor = (_BACKOFF_MULT ** streak) if _BACKOFF_MULT and _BACKOFF_MULT > 1.0 else 1.0
    interval = base_interval * backoff_factor
    if _BACKOFF_MAX_HOURS and _BACKOFF_MAX_HOURS > 0:
        interval = min(interval, _BACKOFF_MAX_HOURS * 3600)
    if _BACKOFF_JITTER_PCT and _BACKOFF_JITTER_PCT > 0:
        jitter = 1.0 + random.uniform(-_BACKOFF_JITTER_PCT, _BACKOFF_JITTER_PCT)
        if jitter < 0.1:
            jitter = 0.1
        interval *= jitter

    next_ts = reference_ts + interval

    start_h = settings.PERSONAL_PING_START_HOUR
    end_h   = settings.PERSONAL_PING_END_HOUR

    def _in_window(h, start_h, end_h):
        return (start_h <= h < end_h) if start_h < end_h else (h >= start_h or h < end_h)

    _next = datetime.fromtimestamp(next_ts, tz)
    next_local = _next.hour + _next.minute / 60.0
    if not _in_window(next_local, start_h, end_h):
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


CLAIM_DUE_LUA = """
-- KEYS[1] = schedule zset
-- ARGV[1] = now_ts, ARGV[2] = batch_size
local now_ts = tonumber(ARGV[1])
local n = tonumber(ARGV[2]) or 50
local due = redis.call('ZRANGEBYSCORE', KEYS[1], 0, now_ts, 'LIMIT', 0, n)
if #due == 0 then return {} end
redis.call('ZREM', KEYS[1], unpack(due))
return due
"""


async def _claim_due(redis, now_ts: float, batch_size: int):

    global _CLAIM_DUE_SHA
    try:
        if _CLAIM_DUE_SHA:
            return await redis.evalsha(_CLAIM_DUE_SHA, 1, PING_SCHEDULE_KEY, now_ts, int(batch_size))
    except RedisError as e:
        if "NOSCRIPT" not in str(e):
            raise
        _CLAIM_DUE_SHA = None
    try:
        _CLAIM_DUE_SHA = await redis.script_load(CLAIM_DUE_LUA)
        return await redis.evalsha(_CLAIM_DUE_SHA, 1, PING_SCHEDULE_KEY, now_ts, int(batch_size))
    except RedisError:
        return await redis.eval(CLAIM_DUE_LUA, 1, PING_SCHEDULE_KEY, now_ts, int(batch_size))


async def personal_ping() -> None:

    redis = get_redis()
    
    MAX_LOOPS = 5
    for _ in range(MAX_LOOPS):
        now = time_module.time()
        try:
            raw = await _claim_due(redis, now, settings.PERSONAL_PING_BATCH_SIZE)
        except RedisError:
            logger.debug("personal_ping: cannot claim schedule")
            return
        if not raw:
            return

        due = [(m.decode() if isinstance(m, (bytes, bytearray)) else str(m)) for m in raw]

        keys = [LAST_PRIVATE_TS_KEY.format(uid) for uid in due]
        try:
            async with redis.pipeline(transaction=False) as pipe:
                for k in keys:
                    pipe.exists(k)
                exists_flags = await pipe.execute()
        except RedisError:
            logger.debug("personal_ping: EXISTS check failed; requeueing due users")
            try:
                when = time_module.time() + 60
                async with redis.pipeline(transaction=True) as pipe:
                    for uid in due:
                        pipe.zadd(PING_SCHEDULE_KEY, {str(uid): when})
                    pipe.expire(PING_SCHEDULE_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
                    await pipe.execute()
            except RedisError:
                logger.exception("personal_ping: requeue after EXISTS failure failed")
            continue
        due = [uid for uid, alive in zip(due, exists_flags) if int(alive) == 1]
        if not due:
            continue

        logger.debug("personal_ping: %d users due (alive)", len(due))
        tasks = []
        for uid_str in due:
            try:
                user_id = int(uid_str)
            except ValueError:
                continue

            async def _safe_handle(uid: int, ref_now: float):
                try:
                    await asyncio.wait_for(_handle_user_ping(uid, uid, ref_now), timeout=75)
                except asyncio.TimeoutError:
                    logger.warning("personal_ping: user %s timed out — rescheduling", uid)
                    try:
                        await _schedule_next_ping(uid, time_module.time())
                    except Exception:
                        logger.exception("personal_ping: reschedule after timeout failed for %s", uid)
                except Exception:
                    logger.exception("personal_ping: error for user %s", uid)
                    try:
                        await _schedule_next_ping(uid, time_module.time())
                    except Exception:
                        logger.exception("personal_ping: reschedule after error failed for %s", uid)
            tasks.append(_safe_handle(user_id, now))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await asyncio.sleep(0)

async def _handle_user_ping(chat_id: int, user_id: int, now: float) -> None:
    
    redis = get_redis()

    try:
        should_reschedule = await _send_contextual_ping(chat_id, user_id)
    except Exception:
        logger.exception("_handle_user_ping: error sending ping for %s", user_id)
        should_reschedule = True
    finally:
        if should_reschedule:
            await _schedule_next_ping(user_id, time_module.time())

async def _send_contextual_ping(chat_id: int, user_id: int) -> bool:

    redis = get_redis()
    try:
        raw_streak = await redis.get(PING_STREAK_KEY.format(user_id))
        if raw_streak is None:
            streak = 0
        else:
            try:
                streak = int(raw_streak.decode() if isinstance(raw_streak, (bytes, bytearray)) else raw_streak)
            except Exception:
                streak = 0
    except RedisError:
        logger.debug("Cannot read ping streak for %s", user_id)
        streak = 0
    if streak >= MAX_CONSECUTIVE_PINGS:
        logger.debug("skip %s: reached max consecutive pings (%d)", user_id, streak)
        return False

    persona = await get_persona(chat_id)
    orig_gender = persona.user_gender

    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
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
    system_msg = await build_system_prompt(persona, guidelines, user_gender=persona.user_gender)

    e = persona.state.get("engagement_mod", 0.5)
    c = persona.state.get("curiosity_mod", 0.5)
    a = persona.state.get("arousal_mod", 0.5)

    boredom = ((1.0 - e) + (1.0 - c) + (1.0 - a)) / 3.0
    if boredom < settings.PERSONAL_PING_MIN_BOREDOM:
        logger.debug("skip %s, boredom=%.2f", user_id, boredom)
        return True

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
                endpoint="responses.create",
                model=settings.RESPONSE_MODEL,
                input=[
                    _msg("system", system_msg),
                    _msg("user", prompt)
                ],
                max_output_tokens=max_tokens,
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
            ),
            timeout=60.0,
        )
        text = (_get_output_text(resp) or "").strip()
    except Exception:
        logger.exception("_send_contextual_ping: OpenAI error for %s", user_id)
        return True
    if not text:
        logger.warning("_send_contextual_ping: empty model output for %s", user_id)
        return True

    try:
        prev_txt = await redis.hget(f"last_ping:pm:{user_id}", "text")
        if isinstance(prev_txt, (bytes, bytearray)):
            prev_txt = prev_txt.decode("utf-8", "ignore")
        if prev_txt and prev_txt.strip() == text:
            logger.debug("skip %s: duplicate ping text", user_id)
            return True
    except RedisError:
        logger.debug("cannot read last_ping to dedupe", exc_info=True)

    logger.info("Generated personal ping for %s (boredom=%.2f)", user_id, boredom)

    try:
        mid = await _send_private_with_retry(user_id, text)
        if not mid:
            return True
        try:
            await redis.set(f"msg:{user_id}:{mid}", text, ex=settings.MEMORY_TTL_DAYS * 86_400)
            await redis.hset(f"last_ping:pm:{user_id}", mapping={
                "msg_id": int(mid),
                "ts": int(time_module.time()),
                "text": text
            })
            await redis.expire(f"last_ping:pm:{user_id}", settings.PERSONAL_PING_RETENTION_SECONDS)
        except RedisError:
            logger.debug("failed to cache PM ping message_id/text", exc_info=True)
        try:
            await push_message(chat_id, "assistant", text, user_id=user_id)
        except Exception:
            logger.exception("push_message failed for personal ping %s", user_id)
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(PING_STREAK_KEY.format(user_id))
                pipe.expire(PING_STREAK_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
                await pipe.execute()
        except RedisError:
            logger.exception("Failed to update ping streak for %s", user_id)
        return True
    except TelegramForbiddenError:
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
                pipe.delete(LAST_PRIVATE_TS_KEY.format(user_id))
                pipe.delete(IDLE_LIST_KEY.format(user_id))
                pipe.delete(PING_STREAK_KEY.format(user_id))
                await pipe.execute()
        except RedisError:
            logger.exception("Failed to cleanup forbidden user %s", user_id)
        logger.info("Removed %s from personal ping (bot forbidden)", user_id)
        return False
    except Exception:
        logger.exception("_send_contextual_ping: error sending ping for %s", user_id)
        return True
    finally:
        persona.user_gender = orig_gender
    
async def _user_zoneinfo(user_id: int) -> ZoneInfo:
    tz_name = None
    try:
        async with AsyncSessionLocal() as db:
            u = await db.get(User, user_id)
            tz_name = getattr(u, "timezone", None)
    except Exception:
        pass
    if not tz_name:
        tz_name = getattr(settings, "DEFAULT_TZ", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")
EOF