cat >app/services/addons/group_ping.py<< 'EOF'
#app/services/addons/group_ping.py

from __future__ import annotations

import html
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
from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
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
-- KEYS[1] = last_ping_zset, KEYS[2] = last_global_key, KEYS[3] = user_last_ts_zset
-- ARGV[1] = max_ping_score (now - user_cooldown)
-- ARGV[2] = now_ts
-- ARGV[3] = ttl
-- ARGV[4] = inactive_before (now - active_recent_seconds)
-- ARGV[5] = scan_limit
local zkey = KEYS[1]
local gkey = KEYS[2]
local ukey = KEYS[3]
local max_score = tonumber(ARGV[1])
local now_ts = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local inactive_before = tonumber(ARGV[4])
local scan_limit = tonumber(ARGV[5])
if not scan_limit or scan_limit < 1 then scan_limit = 50 end
local candidates = redis.call('ZRANGEBYSCORE', zkey, 0, max_score, 'LIMIT', 0, scan_limit)
for i, uid in ipairs(candidates) do
  local last_user_ts = redis.call('ZSCORE', ukey, uid)
  if (not last_user_ts) or (tonumber(last_user_ts) < inactive_before) then
    redis.call('ZADD', zkey, now_ts, uid)
    redis.call('SET', gkey, now_ts, 'EX', ttl)
    return uid
  end
end
return nil
"""


async def _send_with_retry(chat_id: int, text: str) -> int | None:

    attempt = 1
    while True:
        try:
            msg = await bot.send_message(chat_id, text, parse_mode="HTML")
            return int(msg.message_id)
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            logger.warning("RetryAfter %ss on send_message (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)
            attempt += 1
        except TelegramBadRequest as e:
            logger.warning("BadRequest on send_message: %s", e)
            return None
        except TelegramForbiddenError as e:
            logger.warning("Forbidden on send_message: %s", e)
            return None
        except Exception as e:
            if attempt >= 3:
                logger.exception("send_message failed after %d attempts: %s", attempt, e)
                return None
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
        sleeping_cnt = await redis.zcount(
            f"user_last_ts:{chat_id}",
            0,
            now - settings.GROUP_PING_ACTIVE_RECENT_SECONDS,
        )
        if sleeping_cnt == 0:
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
                if _all_users_cache:
                    await redis.zadd(zkey, {str(u): 0 for u in _all_users_cache}, nx=True)
                    await redis.expire(zkey, settings.GROUP_PING_ACTIVE_TTL_SECONDS * 2)
            except RedisError:
                logger.debug("Failed to sync group_ping zset", exc_info=True)
    except RedisError:
        logger.debug("Failed to sync group_ping zset", exc_info=True)

    lock = redis.lock(f"lock:group_ping:{chat_id}", timeout=1, blocking_timeout=0)
    acquired = await lock.acquire()
    if not acquired:
        return
    try:
        max_score = now - settings.GROUP_PING_USER_COOLDOWN_SECONDS
        last_global_key = f"last_global_ping_ts:{chat_id}"
        ttl = int(adaptive * 2)
        inactive_before = now - settings.GROUP_PING_ACTIVE_RECENT_SECONDS
        user_last_key = f"user_last_ts:{chat_id}"
        pick = await redis.eval(
            LUA_PICK_AND_BUMP_AND_SET,
            3,
            zkey,
            last_global_key,
            user_last_key,
            max_score,
            now,
            ttl,
            inactive_before,
            getattr(settings, "GROUP_PING_SCAN_LIMIT", 100),
        )
        if not pick:
            return
        uid = pick.decode() if isinstance(pick, (bytes, bytearray)) else str(pick)

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

    if settings.EMOJI_PING_LIST and random.random() < settings.EMOJI_PING_PROBABILITY:
        emoji = random.choice(settings.EMOJI_PING_LIST)
        ping_text = f"{mention} {emoji}"
        mid = await _send_with_retry(chat_id, ping_text)
        if mid:
            try:
                await redis.set(f"last_message_ts:{chat_id}", _time.time(), ex=settings.GROUP_PING_ACTIVE_TTL_SECONDS)
            except RedisError:
                logger.debug("failed to update last_message_ts after emoji ping", exc_info=True)
            try:
                await redis.set(f"msg:{chat_id}:{mid}", ping_text, ex=settings.MEMORY_TTL_DAYS * 86_400)
                await redis.hset(f"last_ping:{chat_id}:{uid}", mapping={"msg_id": int(mid), "ts": int(_time.time()), "text": ping_text})
                await redis.expire(f"last_ping:{chat_id}:{uid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
            except RedisError:
                logger.debug("failed to cache ping message_id/text", exc_info=True)
            try:
                await push_message(chat_id, "assistant", ping_text, user_id=int(uid))
            except Exception:
                logger.exception("push_message failed for group ping %s", uid)
            try:
                await redis.incr(_METRIC_SENT)
                await redis.expire(_METRIC_SENT, 86_400)
            except RedisError:
                pass
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
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
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

    append = bool(settings.EMOJI_PING_LIST) and (random.random() < settings.EMOJI_APPEND_PROBABILITY)

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

    system_msg = await build_system_prompt(persona, guidelines, user_gender=persona.user_gender)
    if mem_ctx:
        prompt = (
            f"Below is a conversation history with the user within a global group chat:\n{mem_ctx}\n"
            "____________\n"
            "Do NOT quote it; learn it only to think why the talk stopped.\n"
            "Now, based on your internal reasoning, write the user a short message (maximum 2 sentences, up to 35 words) on your behalf that will naturally re-engage them in the conversation.\n"
            "Don't add any comments, placeholders, or internal reasoning in the final message."
        )
    else:
        prompt = (
            "The group chat has been quiet for a while.\n"
            "Write to the user a creative message (max 2 sentences, up to 35 words) on your own behalf to make them want to reply.\n"
            "Don't add any comments, placeholders, or internal reasoning in the final message."
        )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.RESPONSE_MODEL,
                input=[_msg("system", system_msg), _msg("user", prompt),],
                max_output_tokens=max_tokens,
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
            ),
            timeout=60.0
        )
        ping_text = (_get_output_text(resp) or "").strip()
        if not ping_text:
            raise RuntimeError("empty model output")
        ping_text = html.escape(ping_text)
        if append:
            ping_text = f"{ping_text} {random.choice(settings.EMOJI_PING_LIST)}"
        if "@" not in ping_text:
            ping_text = f"{mention} {ping_text}"
        try:
            prev_txt = await redis.hget(f"last_ping:{chat_id}:{uid}", "text")
            if isinstance(prev_txt, (bytes, bytearray)):
                prev_txt = prev_txt.decode("utf-8", "ignore")
            if prev_txt and prev_txt.strip() == ping_text:
                if settings.EMOJI_PING_LIST:
                    ping_text = f"{ping_text} {random.choice(settings.EMOJI_PING_LIST)}"
                else:
                    logger.debug("group_ping: duplicate text for %s, skip", uid)
                    return
        except RedisError:
            logger.debug("group_ping: last_ping dedupe read failed", exc_info=True)
    except Exception:
        try:
            await redis.incr(_METRIC_OPENAI_FAIL)
            await redis.expire(_METRIC_OPENAI_FAIL, 86_400)
        except RedisError:
            pass
        return

    try:
        mid = await _send_with_retry(chat_id, ping_text)
        if mid:
            try:
                await redis.set(f"last_message_ts:{chat_id}", _time.time(), ex=settings.GROUP_PING_ACTIVE_TTL_SECONDS)
            except RedisError:
                logger.debug("failed to update last_message_ts after text ping", exc_info=True)
            try:

                await redis.set(f"msg:{chat_id}:{mid}", ping_text, ex=settings.MEMORY_TTL_DAYS * 86_400)
                await redis.hset(f"last_ping:{chat_id}:{uid}",mapping={
                    "msg_id": int(mid),
                    "ts": int(_time.time()),
                    "text": ping_text
                })
                await redis.expire(f"last_ping:{chat_id}:{uid}", settings.GROUP_PING_ACTIVE_TTL_SECONDS)
            except RedisError:
                logger.debug("failed to cache ping message_id/text", exc_info=True)
            try:
                await push_message(chat_id, "assistant", ping_text, user_id=int(uid))
            except Exception:
                logger.exception("push_message failed for group ping %s", uid)
            try:
                await redis.incr(_METRIC_SENT)
                await redis.expire(_METRIC_SENT, 86_400)
            except RedisError:
                pass
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