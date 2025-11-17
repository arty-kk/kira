# app/services/addons/group_ping.py
from __future__ import annotations

import html
import logging
import asyncio
import random
import statistics
import time as _time
import contextlib
import re
import datetime as _dt

from aiogram.utils.markdown import hlink
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from redis.exceptions import RedisError, ResponseError

from app.core.db import session_scope
from app.core.models import User
from app.core.memory import get_cached_gender
from app.clients.telegram_client import get_bot
from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.config import settings
from app.core.memory import get_redis, load_context, push_message
from app.emo_engine import get_persona
from app.services.responder.prompt_builder import build_system_prompt
from app.services.addons.analytics import record_ping_sent

logger = logging.getLogger(__name__)

bot = get_bot()

_METRIC_INVOC = "metrics:dynamic_ping:invocations"
_METRIC_SENT = "metrics:dynamic_ping:sent"
_METRIC_OPENAI_FAIL = "metrics:dynamic_ping:openai_failures"
_METRIC_SEND_FAIL = "metrics:dynamic_ping:send_failures"

_all_users_cache: dict[int, set[str]] = {}
_all_users_cache_ts: dict[int, float] = {}
_ALL_USERS_CACHE_TTL = getattr(settings, "GROUP_PING_ALL_USERS_CACHE_TTL", 300)

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0
PERSONAL_WINDOW = 100

DEFAULT_MODS = {
    "creativity_mod": 0.5,
    "sarcasm_mod":    0.0,
    "enthusiasm_mod": 0.5,
    "confidence_mod": 0.5,
    "precision_mod":  0.5,
    "fatigue_mod":    0.0,
    "stress_mod":     0.0,
    "valence_mod":    0.0,
}

_GROUP_ARMS = ("emoji", "direct", "callback", "question", "suggestion")

_GROUP_ARM_STATS_KEY = "group_ping_arm_stats:{}:{}"
_GROUP_PENDING_KEY   = "group_pending_ping:{}:{}"

_GROUP_SUCCESS_WINDOW = int(getattr(settings, "GROUP_PING_SUCCESS_WINDOW_SECONDS", 3 * 3600))
_GROUP_ARM_STATS_TTL  = int(getattr(settings, "GROUP_PING_ARM_STATS_TTL_SECONDS", 30 * 24 * 3600))
_GROUP_BANDIT_EPSILON = float(getattr(settings, "GROUP_PING_BANDIT_EPSILON", 0.05))

_GROUP_USER_STATS_KEY = "group_ping_user_stats:{}:{}"
_GROUP_USER_MIN_SCORE = float(getattr(settings, "GROUP_PING_USER_MIN_SCORE", 0.15))
_GROUP_FATIGUE_MAX_CONSEC_FAILS = int(getattr(settings, "GROUP_PING_MAX_CONSEC_FAILS", 5))

_GROUP_HOURLY_ACTIVITY_KEY = "group_ping_hourly_activity:{}"
_GROUP_ACTIVE_HOUR_MIN_RATIO = float(getattr(settings, "GROUP_PING_ACTIVE_HOUR_MIN_RATIO", 0.25))
_GROUP_ACTIVE_HOUR_TTL = int(getattr(settings, "GROUP_PING_ACTIVE_HOUR_TTL", 14 * 24 * 3600))

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


def _bandit_theta(a: int, b: int) -> float:
    a = max(int(a), 1)
    b = max(int(b), 1)
    return random.betavariate(a, b)


async def _group_bandit_get_stats(chat_id: int, uid: str) -> dict[str, tuple[int, int]]:
    redis = get_redis()
    key = _GROUP_ARM_STATS_KEY.format(int(chat_id), str(uid))
    try:
        raw = await redis.hgetall(key)
    except RedisError:
        raw = {}

    data: dict[str, int] = {}
    for k, v in (raw or {}).items():
        try:
            ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            vv = int(v.decode() if isinstance(v, (bytes, bytearray)) else v)
        except Exception:
            continue
        data[ks] = vv

    stats: dict[str, tuple[int, int]] = {}
    for arm in _GROUP_ARMS:
        a = data.get(f"a:{arm}", 1)
        b = data.get(f"b:{arm}", 1)
        if a < 1:
            a = 1
        if b < 1:
            b = 1
        stats[arm] = (a, b)
    return stats

async def _group_user_update(chat_id: int, uid: str | int, success: bool) -> None:

    redis = get_redis()
    key = _GROUP_USER_STATS_KEY.format(int(chat_id), int(uid))
    now_ts = int(_time.time())

    try:
        async with redis.pipeline(transaction=True) as p:
            if success:
                p.hincrby(key, "succ", 1)
                p.hset(key, mapping={"last_succ_ts": now_ts, "consec_fail": 0})
            else:
                p.hincrby(key, "fail", 1)
                p.hincrby(key, "consec_fail", 1)
            if _GROUP_ARM_STATS_TTL > 0:
                p.expire(key, _GROUP_ARM_STATS_TTL)
            await p.execute()
    except RedisError:
        logger.debug("group_user_update failed chat_id=%s uid=%s", chat_id, uid, exc_info=True)


async def _get_user_engagement_score(
    chat_id: int,
    uid: str | int,
    now_ts: float | None = None,
) -> float:

    redis = get_redis()
    key = _GROUP_USER_STATS_KEY.format(int(chat_id), int(uid))
    now_ts = now_ts or _time.time()

    try:
        raw = await redis.hgetall(key)
    except RedisError:
        return 0.5

    if not raw:
        return 0.5

    def _dec(v):
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    data = { _dec(k): _dec(v) for k, v in raw.items() }

    try:
        succ = max(int(data.get("succ", 0)), 0)
    except Exception:
        succ = 0
    try:
        fail = max(int(data.get("fail", 0)), 0)
    except Exception:
        fail = 0
    try:
        consec_fail = max(int(data.get("consec_fail", 0)), 0)
    except Exception:
        consec_fail = 0
    try:
        last_succ_ts = float(data.get("last_succ_ts", 0.0))
    except Exception:
        last_succ_ts = 0.0

    total = succ + fail
    base = (succ / total) if total > 0 else 0.5

    if consec_fail >= _GROUP_FATIGUE_MAX_CONSEC_FAILS:
        base *= 0.2
    else:
        base *= max(0.0, 1.0 - min(consec_fail, 5) * 0.05)

    if last_succ_ts > 0:
        days = max(0.0, (now_ts - last_succ_ts) / 86400.0)
        staleness_penalty = min(days / 14.0, 1.0) * 0.3
        base = max(0.0, base - staleness_penalty)

    return max(0.0, min(1.0, base))


async def _group_bandit_update(chat_id: int, uid: str, arm: str, success: bool) -> None:
    if arm not in _GROUP_ARMS:
        return
    redis = get_redis()
    key = _GROUP_ARM_STATS_KEY.format(int(chat_id), str(uid))
    field = f"a:{arm}" if success else f"b:{arm}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, field, 1)
            if _GROUP_ARM_STATS_TTL > 0:
                pipe.expire(key, _GROUP_ARM_STATS_TTL)
            await pipe.execute()
    except RedisError:
        logger.debug(
            "group_bandit_update failed chat_id=%s uid=%s",
            chat_id,
            uid,
            exc_info=True,
        )

    try:
        await _group_user_update(chat_id, uid, success)
    except Exception:
        logger.debug(
            "group_user_update hook failed chat_id=%s uid=%s",
            chat_id,
            uid,
            exc_info=True,
        )

async def _group_bandit_choose_arm(chat_id: int, uid: str) -> str:
    if random.random() < _GROUP_BANDIT_EPSILON:
        return random.choice(_GROUP_ARMS)
    stats = await _group_bandit_get_stats(chat_id, uid)
    best_arm = None
    best_theta = -1.0
    for arm in _GROUP_ARMS:
        a, b = stats.get(arm, (1, 1))
        theta = _bandit_theta(a, b)
        if theta > best_theta:
            best_theta = theta
            best_arm = arm
    return best_arm or "direct"


async def _group_bandit_mark_pending(chat_id: int, uid: str, ts: int, arm: str) -> None:
    redis = get_redis()
    key = _GROUP_PENDING_KEY.format(int(chat_id), str(uid))
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"ts": int(ts), "arm": arm})
            pipe.expire(key, max(_GROUP_SUCCESS_WINDOW * 2, _GROUP_SUCCESS_WINDOW + 300))
            await pipe.execute()
    except RedisError:
        logger.debug("group_bandit_mark_pending failed chat_id=%s uid=%s", chat_id, uid, exc_info=True)


async def _group_bandit_check_expire(chat_id: int, uid: str, now_ts: float) -> None:
    """Помечаем неуспех, если pending-пинг протух."""
    redis = get_redis()
    key = _GROUP_PENDING_KEY.format(int(chat_id), str(uid))
    try:
        pending = await redis.hgetall(key)
        if not pending:
            return

        def _h(d: dict, k: str):
            return d.get(k) or d.get(k.encode())

        raw_ts = _h(pending, "ts")
        raw_arm = _h(pending, "arm")
        if not raw_ts or not raw_arm:
            await redis.delete(key)
            return

        try:
            p_ts = int(raw_ts.decode() if isinstance(raw_ts, (bytes, bytearray)) else raw_ts)
        except Exception:
            await redis.delete(key)
            return

        arm = raw_arm.decode() if isinstance(raw_arm, (bytes, bytearray)) else str(raw_arm)

        if now_ts > p_ts + _GROUP_SUCCESS_WINDOW:
            await _group_bandit_update(chat_id, uid, arm, success=False)
            await redis.delete(key)
    except RedisError:
        logger.debug("group_bandit_check_expire failed chat_id=%s uid=%s", chat_id, uid, exc_info=True)


async def register_group_activity(chat_id: int, user_id: int) -> None:

    now_ts = _time.time()

    try:
        await _bump_hourly_activity(chat_id, now_ts)
    except Exception:
        logger.debug("bump_hourly_activity from register_group_activity failed chat_id=%s", chat_id, exc_info=True)

    redis = get_redis()
    key = _GROUP_PENDING_KEY.format(int(chat_id), int(user_id))

    try:
        pending = await redis.hgetall(key)
        if not pending:
            return

        def _h(d: dict, k: str):
            return d.get(k) or d.get(k.encode())

        raw_ts = _h(pending, "ts")
        raw_arm = _h(pending, "arm")
        if not raw_ts or not raw_arm:
            await redis.delete(key)
            return

        try:
            p_ts = int(raw_ts.decode() if isinstance(raw_ts, (bytes, bytearray)) else raw_ts)
        except Exception:
            await redis.delete(key)
            return

        arm = raw_arm.decode() if isinstance(raw_arm, (bytes, bytearray)) else str(raw_arm)

        if 0 <= now_ts - p_ts <= _GROUP_SUCCESS_WINDOW:
            await _group_bandit_update(chat_id, user_id, arm, success=True)
            await redis.delete(key)
    except RedisError:
        logger.debug("register_group_activity failed chat_id=%s uid=%s", chat_id, user_id, exc_info=True)

async def _bump_hourly_activity(chat_id: int, now_ts: float) -> None:

    if _GROUP_ACTIVE_HOUR_TTL <= 0:
        return

    redis = get_redis()
    key = _GROUP_HOURLY_ACTIVITY_KEY.format(int(chat_id))
    try:
        try:
            hour = int(_dt.datetime.utcfromtimestamp(now_ts).hour)
        except Exception:
            hour = int(_time.localtime(now_ts).tm_hour)
        hour = max(0, min(23, hour))
        async with redis.pipeline(transaction=True) as p:
            p.hincrby(key, str(hour), 1)
            p.expire(key, _GROUP_ACTIVE_HOUR_TTL)
            await p.execute()
    except RedisError:
        logger.debug("bump_hourly_activity failed chat_id=%s", chat_id, exc_info=True)

async def _is_good_hour_for_ping(chat_id: int, now_ts: float) -> bool:

    if _GROUP_ACTIVE_HOUR_MIN_RATIO <= 0:
        return True

    redis = get_redis()
    key = _GROUP_HOURLY_ACTIVITY_KEY.format(int(chat_id))
    try:
        raw = await redis.hgetall(key)
        if not raw:
            return True

        def _dec(v):
            return v.decode() if isinstance(v, (bytes, bytearray)) else v

        counts: dict[int, int] = {}
        for k, v in raw.items():
            try:
                h = int(_dec(k))
                c = int(_dec(v))
            except Exception:
                continue
            if 0 <= h <= 23 and c > 0:
                counts[h] = c

        if not counts:
            return True

        max_cnt = max(counts.values())
        if max_cnt <= 0:
            return True

        try:
            hour = int(_dt.datetime.utcfromtimestamp(now_ts).hour)
        except Exception:
            hour = int(_time.localtime(now_ts).tm_hour)
        hour = max(0, min(23, hour))
        cur_cnt = counts.get(hour, 0)
        ratio = float(cur_cnt) / float(max_cnt)
        return ratio >= _GROUP_ACTIVE_HOUR_MIN_RATIO
    except RedisError:
        return True


LUA_PICK_AND_BUMP_AND_SET = """
-- KEYS[1] = last_ping_zset, KEYS[2] = last_global_key, KEYS[3] = user_last_ts_zset
-- ARGV[1] = max_ping_score (now - user_cooldown)
-- ARGV[2] = now_ts
-- ARGV[3] = ttl
-- ARGV[4] = inactive_before (now - active_recent_seconds)
-- ARGV[5] = scan_limit
-- ARGV[6] = offset (jitter for fairness)
local zkey = KEYS[1]
local gkey = KEYS[2]
local ukey = KEYS[3]
local max_score = tonumber(ARGV[1])
local now_ts = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local inactive_before = tonumber(ARGV[4])
local scan_limit = tonumber(ARGV[5])
if not scan_limit or scan_limit < 1 then scan_limit = 50 end
local offset = tonumber(ARGV[6]) or 0
if offset < 0 then offset = 0 end
local candidates = redis.call('ZRANGEBYSCORE', zkey, 0, max_score, 'LIMIT', offset, scan_limit)
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
    targets = {int(x) for x in (getattr(settings, "ALLOWED_GROUP_IDS", []) or []) if str(x).strip()}
    for chat_id in targets:
        try:
            await _exec_group_ping(redis, chat_id)
        except Exception:
            logger.exception("group_ping failed for chat %s", chat_id, exc_info=True)


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
        if not await _is_good_hour_for_ping(chat_id, now):
            return
    except Exception:
        logger.debug("is_good_hour_for_ping failed chat_id=%s", chat_id, exc_info=True)

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
        last_ts = _all_users_cache_ts.get(chat_id, 0.0)
        if now - last_ts > _ALL_USERS_CACHE_TTL:
            fresh = await redis.smembers(f"all_users:{chat_id}") or set()
            _all_users_cache[chat_id] = {
                u.decode() if isinstance(u, (bytes, bytearray)) else str(u)
                for u in fresh
            }
            _all_users_cache_ts[chat_id] = now
        cset = _all_users_cache.get(chat_id, set())
        if cset:
            try:
                flagged = await redis.smembers(f"mod_flagged_users:{chat_id}") or set()
                flagged_norm = {
                    v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
                    for v in flagged
                }
                flagged_list = list(flagged_norm)
                active_flagged: set[str] = set()
                stale_flagged: set[str] = set()
                if flagged_list:
                    async with redis.pipeline(transaction=True) as pipe:
                        for uid in flagged_list:
                            pipe.exists(f"mod_flagged_ttl:{chat_id}:{uid}")
                        exists_list = await pipe.execute()
                    for uid, ex in zip(flagged_list, exists_list):
                        (active_flagged if ex else stale_flagged).add(uid)
                if stale_flagged:
                    try:
                        await redis.srem(f"mod_flagged_users:{chat_id}", *list(stale_flagged))
                    except RedisError:
                        pass
                candidates = cset - active_flagged
                if active_flagged:
                    try:
                        await redis.zrem(zkey, *list(active_flagged))
                    except RedisError:
                        pass
                if candidates:
                    await redis.zadd(zkey, {str(u): 0 for u in candidates}, nx=True)
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
            random.randint(0, int(getattr(settings, "GROUP_PING_SCAN_JITTER", 50))),
        )
        if not pick:
            return
        uid = pick.decode() if isinstance(pick, (bytes, bytearray)) else str(pick)

        try:
            if await redis.exists(f"mod_flagged_ttl:{chat_id}:{uid}"):
                async with redis.pipeline(transaction=True) as p:
                    p.zadd(zkey, {uid: 0})
                    p.delete(last_global_key)
                    await p.execute()
                return
        except RedisError:
            pass

    except (RedisError, ResponseError):
        return
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await lock.release()

    try:
        await _group_bandit_check_expire(chat_id, uid, now)
    except Exception:
        logger.debug("group_ping: bandit expire check failed chat_id=%s uid=%s", chat_id, uid, exc_info=True)

    try:
        member = await bot.get_chat_member(chat_id, int(uid))
    except (TelegramBadRequest, TelegramForbiddenError):
        await redis.srem(f"all_users:{chat_id}", uid)
        with contextlib.suppress(RedisError):
            await redis.zrem(zkey, uid)
            await redis.delete(f"last_global_ping_ts:{chat_id}")
        return

    try:
        if await redis.exists(f"mod_flagged_ttl:{chat_id}:{uid}"):
            return
    except RedisError:
        pass

    if member.status in ("left", "kicked"):
        await redis.srem(f"all_users:{chat_id}", uid)
        with contextlib.suppress(RedisError):
            await redis.zrem(zkey, uid)
            await redis.delete(f"last_global_ping_ts:{chat_id}")
        return

    try:
        engagement_score = await _get_user_engagement_score(chat_id, uid, now)
    except Exception:
        engagement_score = 0.5

    if engagement_score < _GROUP_USER_MIN_SCORE:
        logger.debug(
            "group_ping: skip uid=%s due to low engagement_score=%.3f (threshold=%.3f)",
            uid,
            engagement_score,
            _GROUP_USER_MIN_SCORE,
        )
        with contextlib.suppress(RedisError):
            await redis.delete(f"last_global_ping_ts:{chat_id}")
        return

    mention = (
        f"@{member.user.username}"
        if member.user.username
        else hlink(member.user.full_name or uid, f"tg://user?id={uid}")
    )

    persona = await get_persona(chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("group_ping: persona restore failed")

    gender = await get_cached_gender(int(uid))
    if gender not in ("male", "female"):
        async with session_scope(stmt_timeout_ms=2000, read_only=True) as db:
            from sqlalchemy import select
            res = await db.execute(
                select(User.gender).where(User.id == int(uid)).limit(1)
            )
            g = res.scalar_one_or_none()
            if g in ("male", "female"):
                gender = g
    user_gender_val = gender if gender in ("male", "female") else None

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(persona.style_modifiers(), 30)
    except Exception:
        logger.exception("style_modifiers acquisition failed")
        style_mods = {}
    mods = _merge_and_clamp_mods(style_mods)

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
            try:
                txt = re.sub(r"<[^>]+>", "", m.get("content", ""))
            except Exception:
                txt = m.get("content", "")
            blocks.append(f"{who}: {txt}")

        mem_ctx = "\n".join(blocks)
    except Exception:
        logger.exception("load_context failed for chat_id=%s user=%s", chat_id, uid)
        mem_ctx = ""

    valence = persona.state.get("valence_mod", persona.state.get("valence", 0.0))
    arousal = persona.state.get("arousal_mod", persona.state.get("arousal", 0.5))
    try:
        valence = max(-1.0, min(1.0, float(valence)))
    except Exception:
        valence = 0.0
    try:
        arousal = max(0.0, min(1.0, float(arousal)))
    except Exception:
        arousal = 0.5

    if valence > settings.GROUP_PING_MAX_VALENCE or arousal > settings.GROUP_PING_MAX_AROUSAL:
        return

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
    max_tokens = 150
    try:
        dynamic_temperature *= (1.0 + 0.10 * float(mods["valence_mod"]))
    except Exception:
        pass
    if dynamic_temperature < 0.55:
        dynamic_temperature = 0.55
    if dynamic_temperature > 0.70:
        dynamic_temperature = 0.70
    if dynamic_top_p < 0.85:
        dynamic_top_p = 0.85
    if dynamic_top_p > 0.98:
        dynamic_top_p = 0.98

    try:
        logger.info(
            "GROUP_PING sampling: novelty=%.3f coherence=%.3f temp=%.2f top_p=%.2f "
            "mods[c/sa/e/conf/prec/fat/str/val]=[%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f]",
            novelty, coherence, dynamic_temperature, dynamic_top_p,
            mods["creativity_mod"], mods["sarcasm_mod"], mods["enthusiasm_mod"],
            mods["confidence_mod"], mods["precision_mod"], mods["fatigue_mod"],
            mods["stress_mod"], mods["valence_mod"]
        )
    except Exception:
        pass

    system_msg = await build_system_prompt(persona, guidelines, user_gender=user_gender_val)

    try:
        chosen_arm = await _group_bandit_choose_arm(chat_id, uid)
    except Exception:
        logger.debug("group_bandit_choose_arm failed chat_id=%s uid=%s", chat_id, uid, exc_info=True)
        chosen_arm = "direct"

    if chosen_arm == "emoji" and not getattr(settings, "EMOJI_PING_LIST", None):
        chosen_arm = "direct"


    arm_hint_map = {
        "emoji": (
            "Use a minimal, playful nudge: only mention the user and one suitable emoji. "
            "No extra text."
        ),
        "direct": (
            "Write one short, clear nudge that addresses the user personally and invites them "
            "to drop a quick reply. Natural, friendly, no pressure."
        ),
        "callback": (
            "Pick one specific detail from the history with this user in this group and "
            "continue that thread in 1 short message."
        ),
        "question": (
            "Ask exactly ONE simple, concrete question tailored to this user's past messages "
            "so it's very easy to answer."
        ),
        "suggestion": (
            "Suggest ONE tiny topic or action that fits the user's interests or previous talk, "
            "so they are tempted to respond."
        ),
    }
    arm_hint = arm_hint_map.get(chosen_arm, arm_hint_map["direct"])


    if chosen_arm == "emoji":
        emoji = random.choice(settings.EMOJI_PING_LIST)
        ping_text = f"{mention} {emoji}"
        mid = await _send_with_retry(chat_id, ping_text)

        if mid:
            try:
                await redis.set(
                    f"last_message_ts:{chat_id}",
                    _time.time(),
                    ex=settings.GROUP_PING_ACTIVE_TTL_SECONDS,
                )
            except RedisError:
                logger.debug("failed to update last_message_ts after emoji ping", exc_info=True)
            try:
                await redis.set(
                    f"msg:{chat_id}:{mid}",
                    ping_text,
                    ex=settings.MEMORY_TTL_DAYS * 86_400,
                )
                await redis.hset(
                    f"last_ping:{chat_id}:{uid}",
                    mapping={"msg_id": int(mid), "ts": int(_time.time()), "text": ping_text, "arm": "emoji"},
                )
                await redis.expire(
                    f"last_ping:{chat_id}:{uid}",
                    settings.GROUP_PING_ACTIVE_TTL_SECONDS,
                )
            except RedisError:
                logger.debug("failed to cache ping message_id/text", exc_info=True)
            try:
                await push_message(chat_id, "assistant", ping_text, user_id=int(uid))
            except Exception:
                logger.exception("push_message failed for group emoji ping %s", uid)
            try:
                await redis.incr(_METRIC_SENT)
                await redis.expire(_METRIC_SENT, 86_400)
            except RedisError:
                pass
            with contextlib.suppress(Exception):
                asyncio.create_task(record_ping_sent(chat_id, "group:emoji"))
            await _group_bandit_mark_pending(chat_id, uid, int(_time.time()), "emoji")
        else:
            with contextlib.suppress(RedisError):
                await redis.delete(f"last_global_ping_ts:{chat_id}")
            try:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.incr(_METRIC_SEND_FAIL)
                    pipe.expire(_METRIC_SEND_FAIL, 86_400)
                    await pipe.execute()
            except RedisError:
                pass
        return

    if mem_ctx:
        prompt = (
            f"Below is a conversation history with this user inside the group chat:\n"
            f"{mem_ctx}\n"
            "____________\n"
            "Do NOT quote it directly; use it only to understand why the talk stopped.\n"
            f"STRATEGY_HINT: {arm_hint}\n"
            "Write ONE short message (1–2 sentences, up to 35 words) on your behalf "
            "that will naturally re-engage this user in the group.\n"
            "No meta-commentary, no placeholders, no markdown, no emojis unless they fit your style. "
            "Make it feel personal and context-aware."
        )
    else:
        prompt = (
            "The group chat has been quiet for a while.\n"
            f"STRATEGY_HINT: {arm_hint}\n"
            "Write ONE creative, but natural message (1–2 sentences, up to 35 words) "
            "addressed to the selected user to make them want to reply in the group.\n"
            "No meta-commentary, no placeholders, no markdown."
        )

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.RESPONSE_MODEL,
                input=[
                    _msg("system", system_msg),
                    _msg("user", prompt),
                ],
                max_output_tokens=max_tokens,
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
            ),
            timeout=settings.RESPONSE_MODEL_TIMEOUT,
        )
        ping_text = (_get_output_text(resp) or "").strip()
        if not ping_text:
            raise RuntimeError("empty model output")

        ping_text = html.escape(ping_text)

        if "@" not in ping_text and "tg://user?id=" not in ping_text:
            ping_text = f"{mention} {ping_text}"
        else:
            if not ping_text.startswith("@") and "tg://user?id=" not in ping_text[:40]:
                ping_text = f"{mention} {ping_text}"

        try:
            prev_txt = await redis.hget(f"last_ping:{chat_id}:{uid}", "text")
            if isinstance(prev_txt, (bytes, bytearray)):
                prev_txt = prev_txt.decode("utf-8", "ignore")
            if prev_txt and prev_txt.strip() == ping_text:
                if getattr(settings, "EMOJI_PING_LIST", None):
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
            now2 = _time.time()
            try:
                await redis.set(
                    f"last_message_ts:{chat_id}",
                    now2,
                    ex=settings.GROUP_PING_ACTIVE_TTL_SECONDS,
                )
            except RedisError:
                logger.debug("failed to update last_message_ts after text ping", exc_info=True)
            try:
                await redis.set(
                    f"msg:{chat_id}:{mid}",
                    ping_text,
                    ex=settings.MEMORY_TTL_DAYS * 86_400,
                )
                await redis.hset(
                    f"last_ping:{chat_id}:{uid}",
                    mapping={
                        "msg_id": int(mid),
                        "ts": int(now2),
                        "text": ping_text,
                        "arm": chosen_arm,
                    },
                )
                await redis.expire(
                    f"last_ping:{chat_id}:{uid}",
                    settings.GROUP_PING_ACTIVE_TTL_SECONDS,
                )
            except RedisError:
                logger.debug("failed to cache ping message_id/text", exc_info=True)
            try:
                await push_message(chat_id, "assistant", ping_text, user_id=int(uid))
            except Exception:
                logger.exception("push_message failed for group ping %s", uid)
            try:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.incr(_METRIC_SENT)
                    pipe.expire(_METRIC_SENT, 86_400)
                    await pipe.execute()
            except RedisError:
                pass
            with contextlib.suppress(Exception):
                asyncio.create_task(record_ping_sent(chat_id, f"group:{chosen_arm}"))
            await _group_bandit_mark_pending(chat_id, uid, int(now2), chosen_arm)
        else:
            with contextlib.suppress(RedisError):
                await redis.delete(f"last_global_ping_ts:{chat_id}")
            try:
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.incr(_METRIC_SEND_FAIL)
                    pipe.expire(_METRIC_SEND_FAIL, 86_400)
                    await pipe.execute()
            except RedisError:
                pass
    finally:
        pass