cat >app/services/addons/group_ping.py<< 'EOF'
#app/services/addons/group_ping.py

from __future__ import annotations

import logging
import asyncio
import random
import statistics
import time as _time

from aiogram.utils.markdown import hlink
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from redis.exceptions import RedisError, ResponseError

from app.core.db import AsyncSessionLocal
from app.core.models import User
from app.core.memory import get_cached_gender
from app.clients.telegram_client import get_bot
from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core.memory import get_redis, load_context, push_message
from app.emo_engine import get_persona 
from app.services.responder.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

bot = get_bot()

_METRIC_INVOC = "metrics:dynamic_ping:invocations"
_METRIC_SENT = "metrics:dynamic_ping:sent"
_METRIC_OPENAI_FAIL = "metrics:dynamic_ping:openai_failures"
_METRIC_SEND_FAIL = "metrics:dynamic_ping:send_failures"

_all_users_cache: set[str] = set()
_all_users_cache_ts: float = 0.0
_ALL_USERS_CACHE_TTL = getattr(settings, "GROUP_PING_ALL_USERS_CACHE_TTL", 300)

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0
PERSONAL_WINDOW = 100
DEFAULT_MODS = {
    "creativity_mod": 0.5, "sarcasm_mod": 0.0, "enthusiasm_mod": 0.5,
    "confidence_mod": 0.5, "precision_mod": 0.5,
    "fatigue_mod":   0.0, "stress_mod":    0.0,
}

LUA_PICK_AND_BUMP_AND_SET = """
-- KEYS[1] = last_ping_zset, KEYS[2] = last_global_key
-- ARGV[1] = max_score, ARGV[2] = now_ts, ARGV[3] = ttl
local zkey = KEYS[1]
local gkey = KEYS[2]
local max_score = tonumber(ARGV[1])
local now_ts = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local res = redis.call('ZRANGEBYSCORE', zkey, 0, max_score, 'LIMIT', 0, 1)
if not res[1] then return nil end
redis.call('ZADD', zkey, now_ts, res[1])
redis.call('SET', gkey, now_ts, 'EX', ttl)
return res[1]
"""


async def _send_with_retry(chat_id: int, text: str) -> bool:

    attempt = 1
    while True:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            return True
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            logger.warning("RetryAfter %ss on send_message (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)
            attempt += 1
        except TelegramBadRequest as e:
            logger.warning("BadRequest on send_message: %s", e)
            return False
        except TelegramForbiddenError as e:
            logger.warning("Forbidden on send_message: %s", e)
            return False
        except Exception as e:
            if attempt >= 3:
                logger.exception("send_message failed after %d attempts: %s", attempt, e)
                return False
            await asyncio.sleep(1.5 * attempt)
            attempt += 1


async def group_ping() -> None:
    redis = get_redis()
    chat_id = settings.ALLOWED_GROUP_ID
    try:
        await _exec_group_ping(redis, chat_id)
    except Exception:
        logger.exception("group_ping failed", exc_info=True)


async def _exec_group_ping(redis, chat_id: int) -> None:
    
    now = _time.time()

    raw = await redis.get(f"last_message_ts:{chat_id}")
    if not raw:
        await redis.set(f"last_message_ts:{chat_id}", now)
        await redis.expire(f"last_message_ts:{chat_id}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
        return
    last_ts = float(raw)
    idle = now - last_ts

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(_METRIC_INVOC)
            pipe.expire(_METRIC_INVOC, 86_400)
            await pipe.execute()
    except RedisError:
        logger.debug("Failed to update invocation metric", exc_info=True)

    base = settings.GROUP_PING_IDLE_THRESHOLD_SECONDS
    hist_key = f"idle_list:{chat_id}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.lpush(hist_key, idle)
            pipe.ltrim(hist_key, 0, settings.GROUP_PING_HISTORY_COUNT - 1)
            pipe.expire(hist_key, 86_400)
            pipe.lrange(hist_key, 0, -1)
            results = await pipe.execute()
        data = results[-1] or []
        durations = [float(x) for x in data if x is not None]
        median_idle = statistics.median(durations) if durations else base
    except RedisError:
        logger.debug("Failed to update/read idle history", exc_info=True)
        median_idle = base

    adaptive = max(base, median_idle * settings.GROUP_PING_ADAPTIVE_IDLE_MULTIPLIER)
    if idle < adaptive:
        return

    try:
        lg = await redis.get(f"last_global_ping_ts:{chat_id}")
        last_global = float(lg) if lg else 0.0
    except RedisError:
        last_global = 0.0
    if now - last_global < adaptive * 2:
        return

    try:
        sleeping = await redis.zrangebyscore(
            f"user_last_ts:{chat_id}", 0, now - settings.GROUP_PING_ACTIVE_RECENT_SECONDS
        )
        if not sleeping:
            return
    except RedisError:
        return

    zkey = f"last_ping_zset:{chat_id}"
    global _all_users_cache, _all_users_cache_ts
    try:
        if now - _all_users_cache_ts > _ALL_USERS_CACHE_TTL:
            fresh = await redis.smembers(f"all_users:{chat_id}") or set()
            _all_users_cache = {
                u.decode() if isinstance(u, (bytes, bytearray)) else str(u)
                for u in fresh
            }
            _all_users_cache_ts = now
        if _all_users_cache:
            try:
                async with redis.pipeline(transaction=True) as pipe:
                    for u in _all_users_cache:
                        pipe.zadd(zkey, {str(u): 0}, nx=True)
                    pipe.expire(zkey, settings.GROUP_PING_ACTIVE_TTL_SECONDS * 2)
                    await pipe.execute()
            except RedisError:
                logger.debug("Failed to sync group_ping zset", exc_info=True)
    except RedisError:
        logger.debug("Failed to sync group_ping zset", exc_info=True)

    lock = redis.lock(f"lock:group_ping:{chat_id}", timeout=1, blocking_timeout=0)
    acquired = await lock.acquire()
    if not acquired:
        return
    try:
        # atomic pick, bump score and set last_global_ping_ts via Lua
        max_score = now - settings.GROUP_PING_USER_COOLDOWN_SECONDS
        last_global_key = f"last_global_ping_ts:{chat_id}"
        ttl = int(adaptive * 2)
        pick = await redis.eval(
            LUA_PICK_AND_BUMP_AND_SET,
            2,
            zkey,
            last_global_key,
            max_score,
            now,
            ttl
        )
        if not pick:
            return
        uid = pick.decode() if isinstance(pick, (bytes, bytearray)) else str(pick)
        sleeping_str = {
            s.decode() if isinstance(s, (bytes, bytearray)) else str(s)
            for s in sleeping
        }
        if uid not in sleeping_str:
            return
    except (RedisError, ResponseError):
        return
    finally:
        if acquired:
            try:
                await lock.release()
            except Exception:
                logger.debug("group_ping: failed to release lock", exc_info=True)

    try:
        member = await bot.get_chat_member(chat_id, int(uid))
    except (TelegramBadRequest, TelegramForbiddenError):
        await redis.srem(f"all_users:{chat_id}", uid)
        try:
            await redis.zrem(zkey, uid)
        except RedisError:
            pass
        return

    if member.status in ("left", "kicked"):
        await redis.srem(f"all_users:{chat_id}", uid)
        try:
            await redis.zrem(zkey, uid)
        except RedisError:
            pass
        return

    mention = (
        f"@{member.user.username}"
        if member.user.username
        else hlink(member.user.full_name or uid, f"tg://user?id={uid}")
    )

    emoji = random.choice(settings.EMOJI_PING_LIST)
    if random.random() < settings.EMOJI_PING_PROBABILITY:
        ping_text = f"{mention} {emoji}"
        ok = await _send_with_retry(chat_id, ping_text)
        if ok:
            try:
                await push_message(chat_id, "assistant", ping_text, user_id=int(uid))
            except Exception:
                logger.exception("push_message failed for group ping %s", uid)
            await redis.incr(_METRIC_SENT)
            await redis.expire(_METRIC_SENT, 86_400)
        else:
            try:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.incr(_METRIC_SEND_FAIL)
                    pipe.expire(_METRIC_SEND_FAIL, 86_400)
                    await pipe.execute()
            except RedisError:
                pass
        return

    persona = await get_persona(chat_id)
    orig_gender = getattr(persona, "user_gender", "unknown")
    try:
        await persona._restored_evt.wait()
    except Exception:
        logger.exception("group_ping: persona restore failed")

    gender = None
    async with AsyncSessionLocal() as db:
        u = await db.get(User, int(uid))
        if u and u.gender in ("male", "female"):
            gender = u.gender
    if gender is None:
        gender = await get_cached_gender(int(uid))
    persona.user_gender = gender if gender in ("male", "female") else "unknown"
        
    style_mods = await persona.style_modifiers() or {}
    mods = {
        k: (style_mods.get(k) if style_mods.get(k) is not None else v)
        for k, v in DEFAULT_MODS.items()
    }
    guidelines = await persona.style_guidelines(int(uid))

    try:
        history = await load_context(chat_id, int(uid))

        summary: str | None = None
        if history and history[0].get("role") == "system":
            summary = history[0]["content"].replace("Summary:", "").strip()
            history = history[1:]

        username = member.user.username or None

        def _related(msg: dict) -> bool:
            if msg.get("chat_id") != chat_id:
                return False
            if msg.get("user_id") == int(uid):
                return True
            if msg.get("role") == "assistant" and msg.get("user_id") == int(uid):
                return True
            content = msg.get("content", "")
            if username and f"@{username}" in content:
                return True
            if f"tg://user?id={uid}" in content:
                return True
            return False

        personal_msgs = [m for m in history if _related(m)][-PERSONAL_WINDOW:]

        blocks: list[str] = []
        if summary:
            blocks.append(f"Summary: {summary}")
        for m in personal_msgs:
            who = "You" if m.get("user_id") == int(uid) else "Me"
            blocks.append(f"{who}: {m['content']}")

        mem_ctx = "\n".join(blocks)
    except Exception:
        logger.exception("load_context failed for chat_id=%s user=%s", chat_id, uid)
        mem_ctx = ""

    append = random.random() < settings.EMOJI_APPEND_PROBABILITY

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
    valence = persona.state.get("valence_mod", persona.state.get("valence", 0.0))
    arousal = persona.state.get("arousal_mod", persona.state.get("arousal", 0.5))
    if valence > settings.GROUP_PING_MAX_VALENCE or arousal > settings.GROUP_PING_MAX_AROUSAL:
        return

    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_temperature = min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, dynamic_temperature))
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    dynamic_top_p = min(TOP_P_MAX, max(TOP_P_MIN, dynamic_top_p))
    max_tokens = 150

    system_msg = await build_system_prompt(persona, guidelines)
    if mem_ctx:
        prompt = (
            "Below is a conversation history with this user in the group chat. "
            "Do NOT quote it; read it only to recall where the talk stopped.\n\n"
            f"{mem_ctx}\n\n"
            "Figure out roughly where the talk stopped, then write one punchy line "
            "(max 2 sentences, 35 words) in your own voice that nudges the user to reply."
        )
    else:
        prompt = (
            "The group chat has been quiet for a while. "
            "Write one punchy line from yourself (max 2 sentences, 35 words) "
            "to restart the conversation with the user and make them want to reply."
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
        ping_text = resp.choices[0].message.content.strip()
        if append:
            ping_text = f"{ping_text} {emoji}"
        if "@" not in ping_text:
            ping_text = f"{mention} {ping_text}"
    except Exception:
        try:
            await redis.incr(_METRIC_OPENAI_FAIL)
            await redis.expire(_METRIC_OPENAI_FAIL, 86_400)
        except RedisError:
            pass
        return

    try:
        ok = await _send_with_retry(chat_id, ping_text)
        if ok:
            try:
                await push_message(chat_id, "assistant", ping_text, user_id=int(uid))
            except Exception:
                logger.exception("push_message failed for group ping %s", uid)
            await redis.incr(_METRIC_SENT)
            await redis.expire(_METRIC_SENT, 86_400)
        else:
            try:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.incr(_METRIC_SEND_FAIL)
                    pipe.expire(_METRIC_SEND_FAIL, 86_400)
                    await pipe.execute()
            except RedisError:
                pass
    finally:
        persona.user_gender = orig_gender
EOF