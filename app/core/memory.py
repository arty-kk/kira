#app/core/memory.py
from __future__ import annotations

import contextvars
import json
import re
import logging
import asyncio
import unicodedata
import time as time_module

from typing import Any, Dict, List, Optional, Union
from redis.asyncio import Redis, from_url

from app.config import settings

logger = logging.getLogger(__name__)

_redis_ctx: contextvars.ContextVar[dict[int, dict[str, "SafeRedis"]]] = contextvars.ContextVar("redis_client_map")

_LUA_SET_IF_NEWER = """
local key = KEYS[1]
local newv = ARGV[1]
local ttl  = tonumber(ARGV[2])
local cur  = redis.call('GET', key)
if not cur then
  redis.call('SET', key, newv, 'EX', ttl)
  return 1
end
local function ts_of(s)
  local ok, obj = pcall(cjson.decode, s)
  if not ok or type(obj) ~= 'table' then return 0 end
  local ts = obj['ts']
  if type(ts) ~= 'number' then return 0 end
  return ts
end
local cts = ts_of(cur)
local nts = ts_of(newv)
if nts > cts then
  redis.call('SET', key, newv, 'EX', ttl)
  return 1
end
return 0
"""

def _b2s(x: Any) -> Any:
    return x.decode("utf-8", "ignore") if isinstance(x, (bytes, bytearray)) else x


def extract_summary_data(raw: Optional[Union[str, bytes, bytearray]]) -> str:
    if not raw:
        return ""
    try:
        s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        obj = json.loads(s)
        if isinstance(obj, dict) and "data" in obj:
            return obj["data"]
    except Exception:
        pass
    return raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw


def pack_summary_data(data: str) -> str:
    return json.dumps({"ts": time_module.time(), "data": data}, ensure_ascii=False)


async def set_summary_if_newer(key: str, packed_payload: str, ttl: int) -> bool:
    redis = get_redis()
    try:
        ttl = max(1, int(ttl))
        res = await redis.eval(_LUA_SET_IF_NEWER, 1, key, packed_payload, ttl)
        return bool(int(res or 0))
    except Exception:
        logger.exception("set_summary_if_newer failed for %s", key)
        return False


class SafeRedis:

    def __init__(self, client: Redis, attempts: int = 3) -> None:
        self._client = client
        self._attempts = attempts

    def __getattr__(self, name):
        orig = getattr(self._client, name)
        if not asyncio.iscoroutinefunction(orig):
            return orig

        async def _wrapper(*args, **kwargs):
            for n in range(self._attempts):
                try:
                    return await orig(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if n == self._attempts - 1:
                        logger.exception(
                            "Redis cmd %s failed after %d attempts: %s",
                            name, self._attempts, exc,
                        )
                        raise
                    logger.warning(
                        "Redis cmd %s failed (try %d/%d): %s",
                        name, n + 1, self._attempts, exc,
                    )
                    await asyncio.sleep(0.5 * (2 ** n))
        return _wrapper

    def pipeline(self, *args, **kwargs):
        raw_pipe = self._client.pipeline(*args, **kwargs)

        class SafePipeline:
            def __init__(self, pipe, attempts: int) -> None:
                self._pipe = pipe
                self._attempts = attempts

            def __getattr__(self, item):
                return getattr(self._pipe, item)

            async def execute(self):
                for n in range(self._attempts):
                    try:
                        return await self._pipe.execute()
                    except Exception as exc:
                        if n == self._attempts - 1:
                            logger.exception(
                                "Redis pipeline failed after %d attempts: %s",
                                self._attempts, exc,
                            )
                            raise
                        logger.warning(
                            "Redis pipeline failed (try %d/%d): %s",
                            n + 1, self._attempts, exc,
                        )
                        await asyncio.sleep(0.5 * (2 ** n))

            async def __aenter__(self):
                await self._pipe.__aenter__()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return await self._pipe.__aexit__(exc_type, exc, tb)

        return SafePipeline(raw_pipe, self._attempts)

def _url_for(name: str) -> str:
    if name == "queue":
        return getattr(settings, "REDIS_URL_QUEUE", settings.REDIS_URL)
    if name == "vector":
        return getattr(settings, "REDIS_URL_VECTOR", getattr(settings, "REDIS_URL", "redis://localhost:6379/0"))
    return settings.REDIS_URL

def _decode_for(name: str) -> bool:
    return True if name == "queue" else False

def _create_client(name: str) -> Redis:
    url = _url_for(name)
    kwargs = dict(
        decode_responses=_decode_for(name),
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        health_check_interval=30,
        retry_on_timeout=True,
    )

    pwd = getattr(settings, "REDIS_PASSWORD", None)
    if pwd:
        kwargs.setdefault("password", pwd)
        kwargs.setdefault("username", getattr(settings, "REDIS_USERNAME", "default"))
    return from_url(url, **kwargs)

def get_redis(name: str = "default") -> SafeRedis:

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop_policy().get_event_loop()

    loop_id = id(loop)

    try:
        mapping = _redis_ctx.get()
    except LookupError:
        mapping = {}
    per_loop = mapping.get(loop_id) or {}
    client = per_loop.get(name)

    if client is None:
        raw = _create_client(name)
        client = SafeRedis(raw)
        per_loop[name] = client
        mapping[loop_id] = per_loop
        _redis_ctx.set(mapping)

    return client

def get_redis_queue() -> SafeRedis:
    return get_redis("queue")

def get_redis_vector() -> SafeRedis:
    return get_redis("vector")

async def close_redis_pools() -> None:

    mapping: dict[int, dict[str, SafeRedis]] = _redis_ctx.get({})
    for per_loop in mapping.values():
        for client in per_loop.values():
            raw: Redis = getattr(client, "_client", client)
            try:
                try:
                    await raw.connection_pool.disconnect(inuse_connections=True)
                except TypeError:
                    raw.connection_pool.disconnect(inuse_connections=True)
            except RuntimeError:
                pass
            except Exception as exc:
                logger.warning("Failed to close Redis pool: %s", exc)
    _redis_ctx.set({})


def _k_g_msgs(chat_id: int, user_id: int) -> str:
    return f"mem:g:{chat_id}:u:{user_id}:msgs"


def _k_g_msgs_all(chat_id: int) -> str:
    return f"mem:g:{chat_id}:_all:msgs"


def _k_g_sum(chat_id: int) -> str:
    return f"mem:g:{chat_id}:summary"


def _k_g_sum_u(chat_id: int, user_id: int) -> str:
    return f"mem:g:{chat_id}:u:{user_id}:summary"


def _k_p_msgs(user_id: int) -> str:
    return f"mem:p:{user_id}:msgs"


def _k_p_sum(user_id: int) -> str:
    return f"mem:p:{user_id}:summary"

def _key_user_last_ts(chat_id: int) -> str:
    return f"user_last_ts:{chat_id}"


def _key_last_message_ts(chat_id: int) -> str:
    return f"last_message_ts:{chat_id}"


SHORT_LIMIT: int = settings.SHORT_MEMORY_LIMIT
MEMORY_TTL: int = settings.MEMORY_TTL_DAYS * 86_400


async def is_spam(chat_id: int, user_id: int) -> bool:
    window = settings.SPAM_WINDOW
    limit = settings.SPAM_LIMIT
    key = f"spam:{chat_id}"
    redis = get_redis()

    try:
        count = await redis.hincrby(key, str(user_id), 1)
        if count == 1:
            await redis.expire(key, window + 2)
    except Exception:
        logger.exception("is_spam error for chat %s", chat_id)
        return False

    return count > limit


async def push_message(
    chat_id: int,
    role: str,
    content: str,
    *,
    user_id: int,
) -> None:

    is_private = chat_id == user_id
    key_log = _k_p_msgs(user_id) if is_private else _k_g_msgs(chat_id, user_id)

    entry: Dict[str, Any] = {
        "role": role,
        "content": content,
        "chat_id": chat_id,
        "user_id": user_id,
        "ts": time_module.time(),
    }

    redis = get_redis()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key_log, json.dumps(entry, ensure_ascii=False))
            pipe.expire(key_log, MEMORY_TTL)
            pipe.llen(key_log)
            length_all = None
            key_all = None
            if not is_private:
                key_all = _k_g_msgs_all(chat_id)
                pipe.rpush(key_all, json.dumps(entry, ensure_ascii=False))
                pipe.expire(key_all, MEMORY_TTL)
                pipe.llen(key_all)
            res = await pipe.execute()
            length = res[2]
            if not is_private:
                length_all = res[5]
    except Exception:
        logger.exception("push_message error for chat %s", chat_id)
        return

    if length >= SHORT_LIMIT:
        flag = f"{key_log}:_summary_enqueued"
        try:
            if await redis.set(flag, 1, nx=True, ex=int(getattr(settings, "SUMMARY_GUARD_EX", 120))):
                from app.tasks.celery_app import celery
                if is_private:
                    celery.send_task("summarize_private_old", args=[user_id, length])
                else:
                    celery.send_task("summarize_group_old", args=[chat_id, user_id, length])
        except Exception:
            logger.exception("Failed to enqueue summarize task or set guard flag")

    if (not is_private) and (length_all is not None) and (length_all >= SHORT_LIMIT) and key_all:
        flag_all = f"{key_all}:_summary_enqueued"
        try:
            if await redis.set(flag_all, 1, nx=True, ex=int(getattr(settings, "SUMMARY_GUARD_EX", 120))):
                from app.tasks.celery_app import celery
                celery.send_task("summarize_group_old", args=[chat_id, 0, length_all])
        except Exception:
            logger.exception("Failed to enqueue summarize task (group global)")

async def load_context(
    chat_id: int,
    user_id: int,
) -> List[Dict[str, Any]]:

    is_private = chat_id == user_id
    redis = get_redis()

    if is_private:
        key_sum = _k_p_sum(user_id)
        key_log = _k_p_msgs(user_id)
    else:
        key_sum = _k_g_sum_u(chat_id, user_id)
        key_log = _k_g_msgs(chat_id, user_id)

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.get(key_sum)
            pipe.lrange(key_log, -SHORT_LIMIT, -1)
            summary, rows = await pipe.execute()
    except Exception:
        logger.exception("load_context error chat=%s user=%s", chat_id, user_id)
        return []

    ctx: List[Dict[str, Any]] = []
    if summary:
        summary = extract_summary_data(summary)
        ctx.append({"role": "system", "content": f"Summary: {summary}"})

    for r in rows or []:
        try:
            s = _b2s(r)
            ctx.append(json.loads(s))
        except json.JSONDecodeError:
            logger.warning("Bad JSON in memory chat=%s: %s", chat_id, r)

    def _n(s: str) -> str:
        s = unicodedata.normalize("NFKC", s or "")
        s = re.sub(r"\s+", " ", s).strip()
        return s.casefold()

    dedup: List[Dict[str, Any]] = []
    prev_user_text: str | None = None

    for m in ctx:
        if m.get("role") == "user":
            cur = _n(m.get("content", ""))
            if prev_user_text is not None and cur == prev_user_text:
                continue
            prev_user_text = cur
        else:
            prev_user_text = None
        dedup.append(m)
    return dedup


async def inc_msg_count(chat_id: int) -> None:
    redis = get_redis()
    try:
        await redis.incr(f"msg_count:{chat_id}")
        await redis.expire(f"msg_count:{chat_id}", 86_400)
    except Exception:
        logger.exception("inc_msg_count error chat=%s", chat_id)


async def record_activity(chat_id: int, user_id: int) -> None:
    redis = get_redis()
    now = time_module.time()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.sadd(f"all_users:{chat_id}", user_id)
            pipe.expire(f"all_users:{chat_id}", MEMORY_TTL)
            pipe.zadd(f"user_last_ts:{chat_id}", {user_id: now})
            pipe.expire(f"user_last_ts:{chat_id}", MEMORY_TTL)
            pipe.set(f"last_message_ts:{chat_id}", now)
            pipe.expire(f"last_message_ts:{chat_id}", MEMORY_TTL)
            await pipe.execute()
    except Exception:
        logger.exception("record_activity error chat=%s user=%s", chat_id, user_id)


async def get_cached_gender(user_id: int) -> Optional[str]:
    redis = get_redis()
    raw = await redis.hgetall(f"user_gender_counts:{user_id}") or {}
    counts: Dict[str, int] = {}
    for g, c in (raw.items() if isinstance(raw, dict) else []):
        sg = _b2s(g)
        if sg in ("male", "female"):
            try:
                counts[sg] = int(_b2s(c))
            except Exception:
                pass
    return max(counts, key=counts.get) if counts else None


async def cache_gender(user_id: int, value: str) -> None:
    if value not in ("male", "female"):
        return
    redis = get_redis()
    try:
        await redis.hincrby(f"user_gender_counts:{user_id}", value, 1)
        await redis.expire(f"user_gender_counts:{user_id}", MEMORY_TTL)
    except Exception:
        logger.exception("cache_gender failed user=%s", user_id)
