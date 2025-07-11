#app/services/group_ping.py

from __future__ import annotations

import logging
import asyncio
import random
import statistics

from aiogram.utils.markdown import hlink
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from redis.exceptions import RedisError, ResponseError

from app.clients.telegram_client import get_bot
from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core import get_redis, load_context
from app.emo_engine.registry import get_persona 
from app.services.responder.prompt_builder import build_system_prompt

logger = logging.getLogger(__name__)

bot = get_bot()

_METRIC_INVOC = "metrics:dynamic_ping:invocations"
_METRIC_SENT = "metrics:dynamic_ping:sent"
_METRIC_OPENAI_FAIL = "metrics:dynamic_ping:openai_failures"
_METRIC_SEND_FAIL = "metrics:dynamic_ping:send_failures"

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.5
TOP_P_MIN = 0.7
TOP_P_MAX = 1.0

LUA_PICK_AND_UPDATE = """
local zkey, max_score, now = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2])
local res = redis.call('ZRANGEBYSCORE', zkey, 0, max_score, 'LIMIT', 0, 1)
if not res[1] then return nil end
redis.call('ZADD', zkey, now, res[1])
return res[1]
"""

async def group_ping() -> None:
    redis = get_redis()
    chat_id = settings.ALLOWED_GROUP_ID
    lock_key = f"lock:group_ping:{chat_id}"
    try:
        async with redis.lock(lock_key, timeout=settings.PING_INTERVAL_MINUTES*60):
            await _exec_group_ping(redis, chat_id)
    except AttributeError:
        lock = redis.lock(lock_key, timeout=settings.PING_INTERVAL_MINUTES*60)
        got = await lock.acquire(blocking=False)
        if not got:
            return
        try:
            await _exec_group_ping(redis, chat_id)
        finally:
            lock.release()


async def _exec_group_ping(redis, chat_id: int) -> None:
    
    import time as _time
    
    now = _time.time()

    raw = await redis.get(f"last_message_ts:{chat_id}")
    if not raw:
        await redis.set(f"last_message_ts:{chat_id}", now)
        await redis.expire(f"last_message_ts:{chat_id}", settings.ACTIVE_TTL_SECONDS)
        return
    last_ts = float(raw)
    idle = now - last_ts

    try:
        await redis.incr(_METRIC_INVOC)
        await redis.expire(_METRIC_INVOC, 86_400)
    except RedisError:
        pass

    base = settings.PING_IDLE_THRESHOLD_SECONDS
    hist_key = f"idle_list:{chat_id}"
    try:
        await redis.lpush(hist_key, idle)
        await redis.ltrim(hist_key, 0, settings.PING_HISTORY_COUNT - 1)
        await redis.expire(hist_key, 86_400)
        data = await redis.lrange(hist_key, 0, -1)
        durations = [float(x) for x in data]
        median_idle = statistics.median(durations) if len(durations) >= settings.PING_HISTORY_COUNT else base
    except RedisError:
        median_idle = base

    adaptive = max(base, median_idle * settings.ADAPTIVE_IDLE_MULTIPLIER)
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
            f"user_last_ts:{chat_id}", 0, now - settings.ACTIVE_RECENT_SECONDS
        )
        if not sleeping:
            return
    except RedisError:
        return

    zkey = f"last_ping_zset:{chat_id}"
    try:
        all_u = await redis.smembers(f"all_users:{chat_id}")
        zmem = await redis.zrange(zkey, 0, -1)
        new_members = set(all_u) - set(zmem)
        if new_members:
            mapping = {u: now for u in new_members}
            await redis.zadd(zkey, mapping)
    except RedisError:
        pass

    try:
        max_score = now - settings.PING_USER_COOLDOWN_SECONDS
        pick = await redis.eval(LUA_PICK_AND_UPDATE, 1, zkey, max_score, now)
        if not pick:
            return
        uid = str(pick)
        if uid not in sleeping:
            return
        await redis.set(f"last_global_ping_ts:{chat_id}", now, ex=int(adaptive * 2))
    except (RedisError, ResponseError):
        return

    try:
        member = await bot.get_chat_member(chat_id, int(uid))
    except TelegramBadRequest:
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
        try:
            await bot.send_message(chat_id, ping_text, parse_mode="HTML")
            await redis.incr(_METRIC_SENT)
            await redis.expire(_METRIC_SENT, 86_400)
        except (TelegramBadRequest, TelegramForbiddenError, RedisError, Exception):
            try:
                await redis.incr(_METRIC_SEND_FAIL)
                await redis.expire(_METRIC_SEND_FAIL, 86_400)
            except RedisError:
                pass
        return

    persona = get_persona(int(uid))
    persona.style_modifiers()
    guidelines = await persona.style_guidelines(int(uid))

    try:
        history = await load_context(chat_id)
        mem_ctx = "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in history)
    except Exception:
        logger.exception("load_context failed for chat_id=%s", chat_id)
        mem_ctx = ""

    append = random.random() < settings.EMOJI_APPEND_PROBABILITY

    mods = persona._mods_cache
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
    arousal = persona.state.get("arousal_mod", persona.state.get("arousal", 0.0))
    if valence > settings.GROUP_PING_MAX_VALENCE or arousal > settings.GROUP_PING_MAX_AROUSAL:
        return

    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_temperature = min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, dynamic_temperature))
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    dynamic_top_p = min(TOP_P_MAX, max(TOP_P_MIN, dynamic_top_p))
    max_tokens = 150

    system_msg = build_system_prompt(persona, guidelines)
    prompt = (
        (f"Previously you and the person had the following conversation:\n{mem_ctx}\n\n" if mem_ctx else "")
        + "Write a natural, short message (as people do when they want to start a conversation again) on your behalf."
        + "Respond with only the final text, no explanations, comments, or framing."
    )

    try:
        resp = await _call_openai_with_retry(
            model=settings.RESPONSE_MODEL,
            messages=[system_msg, {"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=dynamic_temperature,
            top_p=dynamic_top_p,
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
        await bot.send_message(chat_id, ping_text, parse_mode="HTML")
        await redis.incr(_METRIC_SENT)
        await redis.expire(_METRIC_SENT, 86_400)
    except (TelegramBadRequest, TelegramForbiddenError, Exception):
        try:
            await redis.incr(_METRIC_SEND_FAIL)
            await redis.expire(_METRIC_SEND_FAIL, 86_400)
        except RedisError:
            pass
