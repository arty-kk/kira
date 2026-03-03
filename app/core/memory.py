#app/core/memory.py
from __future__ import annotations

import json
import logging
import asyncio
from collections import deque
import re
import time
import inspect
import unicodedata
import threading
import time as time_module
import weakref

from typing import Any, Dict, List, Optional, Union
from redis.asyncio import Redis, from_url
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.dialog_logger import log_user_message, log_bot_message
from app.config import settings

logger = logging.getLogger(__name__)

SCAN_COUNT: int = int(getattr(settings, "CLEANUP_REDIS_SCAN_COUNT", 1000))

_redis_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, "SafeRedis"]] = weakref.WeakKeyDictionary()
_redis_lock = threading.Lock()
_local_spam: Dict[tuple[int, int], tuple[int, float]] = {}
_local_spam_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_local_spam_locks_guard = threading.Lock()
_local_spam_guard = threading.Lock()
_local_spam_order: deque[tuple[float, tuple[int, int]]] = deque()


def _get_local_spam_lock_for_current_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _local_spam_locks_guard:
        lock = _local_spam_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _local_spam_locks[loop] = lock
        return lock

def _get_default_tz():
    try:
        return ZoneInfo(getattr(settings, "DEFAULT_TZ", "UTC") or "UTC")
    except Exception:
        return timezone.utc
        
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
    try:
        api_call_to = float(getattr(settings, "API_CALL_TIMEOUT_SEC", 60))
        if name == "queue":
            q_sock_to = float(getattr(settings, "REDIS_QUEUE_SOCKET_TIMEOUT", api_call_to + 10.0))
            kwargs["socket_timeout"] = q_sock_to
        else:
            kwargs["socket_timeout"] = float(getattr(settings, "REDIS_SOCKET_TIMEOUT", 3.0))
        kwargs["socket_connect_timeout"] = float(getattr(settings, "REDIS_SOCKET_CONNECT_TIMEOUT", 3.0))
        kwargs["socket_keepalive"] = True
    except Exception:
        pass
    pwd = getattr(settings, "REDIS_PASSWORD", None)
    if pwd:
        kwargs.setdefault("password", pwd)
        kwargs.setdefault("username", getattr(settings, "REDIS_USERNAME", "default"))
    return from_url(url, **kwargs)

WARN_MS = int(getattr(settings, "REDIS_SLOW_WARN_MS", 100))

READONLY_OR_IDEMPOTENT_COMMANDS = {
    "get", "mget", "hget", "hgetall", "lrange", "llen", "ttl", "exists", "zrange", "smembers", "scan", "scan_iter",
    "delete", "unlink", "zrem", "hdel", "srem",
}

NON_IDEMPOTENT_COMMANDS = {
    "incr", "incrby", "hincrby", "rpush", "lpop", "sadd", "zadd", "expire", "set", "eval", "evalsha",
}


def _is_transport_or_timeout_error(exc: Exception) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, RedisTimeoutError, RedisConnectionError))

class SafeRedis:
    def __init__(self, client: Redis, attempts: int = 3) -> None:
        self._client = client
        self._attempts = attempts

    def __getattr__(self, name):
        orig = getattr(self._client, name)
        if not asyncio.iscoroutinefunction(orig):
            return orig

        cmd_name = (name or "").lower()
        is_retry_allowed = cmd_name in READONLY_OR_IDEMPOTENT_COMMANDS
        is_known_non_idempotent = cmd_name in NON_IDEMPOTENT_COMMANDS
        max_attempts = self._attempts if is_retry_allowed else 1

        async def _wrapper(*args, **kwargs):
            for n in range(max_attempts):
                start = time.perf_counter()
                try:
                    return await orig(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (is_known_non_idempotent or not is_retry_allowed) and _is_transport_or_timeout_error(exc):
                        logger.warning(
                            "Redis cmd %s failed: retry skipped due to non-idempotent semantics (%s)",
                            name,
                            exc,
                        )
                        raise
                    if n == max_attempts - 1:
                        logger.exception(
                            "Redis cmd %s failed after %d attempts: %s",
                            name, max_attempts, exc,
                        )
                        raise
                    logger.warning(
                        "Redis cmd %s failed (try %d/%d): %s",
                        name, n + 1, max_attempts, exc,
                    )
                    await asyncio.sleep(0.5 * (2 ** n))
                finally:
                    dt_ms = (time.perf_counter() - start) * 1000
                    if dt_ms > WARN_MS:
                        def _safe_arg(a):
                            try:
                                s = repr(a)
                            except Exception:
                                s = "<unrepr>"
                            if len(s) > 64:
                                s = s[:64] + "…"
                            s = re.sub(r'(["\'])(?:(?=(\\?))\2.)*?\1', '"***"', s)
                            return s
                        if name == "eval" and args:
                            try:
                                numkeys = int(args[1]) if len(args) >= 2 else 0
                                total_extra = max(0, len(args) - 2)
                                keys = min(numkeys, total_extra)
                                argv = max(0, total_extra - keys)
                                arg_preview = f"<LUA>, numkeys={numkeys}, keys={keys}, argv={argv}"
                            except Exception:
                                arg_preview = "<LUA>"
                        else:
                            arg_preview = ", ".join(_safe_arg(a) for a in args[:3])
                        logger.warning("Redis slow: %s %.1f ms args=%s", name, dt_ms, arg_preview)
        return _wrapper

    def pipeline(self, *args, **kwargs):
        raw_pipe = self._client.pipeline(*args, **kwargs)

        class SafePipeline:

            def __init__(self, pipe) -> None:
                self._pipe = pipe

            def __getattr__(self, item):
                return getattr(self._pipe, item)

            async def execute(self):
                start = time.perf_counter()
                try:
                    return await self._pipe.execute()
                finally:
                    dt_ms = (time.perf_counter() - start) * 1000
                    if dt_ms > WARN_MS:
                        logger.warning("Redis slow: pipeline %.1f ms", dt_ms)

            async def __aenter__(self):
                await self._pipe.__aenter__()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return await self._pipe.__aexit__(exc_type, exc, tb)

        return SafePipeline(raw_pipe)

def get_redis(name: str = "default") -> SafeRedis:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError(
            "get_redis() requires an active asyncio event loop; call it from async context"
        ) from exc

    with _redis_lock:
        if loop.is_closed():
            _redis_by_loop.pop(loop, None)
            raise RuntimeError("get_redis() called on a closed asyncio loop")
        per_loop = _redis_by_loop.setdefault(loop, {})
        client = per_loop.get(name)
        if client is None:
            client = SafeRedis(_create_client(name))
            per_loop[name] = client
        return client

def get_redis_queue() -> SafeRedis:
    return get_redis("queue")

def get_redis_vector() -> SafeRedis:
    return get_redis("vector")

async def close_redis_pools() -> None:
    with _redis_lock:
        loop_mapping = list(_redis_by_loop.items())
        _redis_by_loop.clear()

    for loop, per_loop in loop_mapping:
        loop_closed = loop.is_closed()
        for client in per_loop.values():
            raw: Redis = getattr(client, "_client", client)
            try:
                pool = getattr(raw, "connection_pool", None)
                if pool is not None:
                    disc = getattr(pool, "disconnect", None)
                    try:
                        if callable(disc):
                            if loop_closed:
                                continue
                            try:
                                result = disc()
                                if inspect.isawaitable(result):
                                    await result
                            except TypeError:
                                try:
                                    if loop_closed:
                                        continue
                                    result = disc(inuse_connections=True)
                                    if inspect.isawaitable(result):
                                        await result
                                except Exception:
                                    pass
                    except Exception:
                        logger.debug("close_redis_pools: pool.disconnect() failed", exc_info=True)
                close = getattr(raw, "close", None)
                if close is not None:
                    try:
                        if callable(close):
                            if loop_closed:
                                continue
                            result = close()
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        logger.debug("close_redis_pools: client.close() failed", exc_info=True)
            except Exception as exc:
                logger.warning("Failed to close Redis pool: %s", exc)


def _b2s(x: Any, default: Any = None) -> Any:
    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8", "ignore")
        except Exception:
            return default if default is not None else x
    return x

def approx_tokens(s: str) -> int:
    try:
        cpt = float(getattr(settings, "APPROX_CHARS_PER_TOKEN", 3.8))
    except Exception:
        cpt = 3.8
    return max(1, int(len(s or "") / max(0.1, cpt)))

def _days(name: str, fallback_days: int) -> int:
    try:
        return int(getattr(settings, name, fallback_days))
    except Exception:
        return int(fallback_days)

_ttl_stm_mtm_days = _days("MEMORY_TTL_DAYS_STM_MTM", _days("MEMORY_TTL_DAYS", 30))
_ttl_ltm_days = _days("MEMORY_TTL_DAYS_LTM", 365)
_memtxt_ttl_days = getattr(settings, "MEMTXT_TTL_DAYS", None)
try:
    if _memtxt_ttl_days is None:
        _memtxt_ttl_days = _days("MEMORY_TTL_DAYS", 7)
    else:
        _memtxt_ttl_days = int(_memtxt_ttl_days)
except Exception:
    _memtxt_ttl_days = _days("MEMORY_TTL_DAYS", 7)
try:
    _memtxt_seen_ttl = int(getattr(settings, "MEMTXT_SEEN_TTL", 86_400))
except Exception:
    _memtxt_seen_ttl = 86_400

MEMORY_TTL_STM_MTM: int = max(1, _ttl_stm_mtm_days) * 86_400
MEMORY_TTL_LTM: int = max(1, _ttl_ltm_days) * 86_400
MEMORY_TTL: int = MEMORY_TTL_STM_MTM
MEMTXT_TTL_DAYS: int = max(0, int(_memtxt_ttl_days))
MEMTXT_SEEN_TTL: int = max(1, int(_memtxt_seen_ttl))
USER_KEYS_REGISTRY_TTL: int = max(
    MEMORY_TTL_STM_MTM,
    MEMORY_TTL_LTM,
    MEMTXT_TTL_DAYS * 86_400,
    MEMTXT_SEEN_TTL,
)

def _ns_prefix(namespace: str) -> str:
    return "api:" if namespace == "api" else ""

def _user_keys_registry(user_id: int, namespace: str | None = None) -> str:
    prefix = _ns_prefix(namespace) if namespace else ""
    return f"{prefix}user:keys:{user_id}"

async def _register_user_key(redis: SafeRedis, user_id: int, key: str, ttl_sec: int) -> None:
    """
    Registry for per-user keys (used by delete_user_redis_data). Keys recorded here include:
    - mem:stm:*, mem:mtm:*, mem:mtm_tokens:*, mem:mtm_recent:*, mem:mtm_recent_tokens:*
    - mem:ltm:*, mem:ltm_slices:*
    - user_gender_counts:{user_id}, last_user_ts:{user_id}, last_private_ts:{user_id}
    - memory:ids:{chat}:{uid}, memtxt:ids:{chat}:{uid}, memtxt:seen:{chat}:{uid}
    Registry TTL is fixed to USER_KEYS_REGISTRY_TTL (max of TTLs for registered keys) so it
    won't expire before any long-lived per-user keys.
    """
    ttl = max(1, int(ttl_sec))
    registry_key = _user_keys_registry(
        user_id,
        "api" if str(key).startswith("api:") else None,
    )
    async with redis.pipeline(transaction=False) as pipe:
        pipe.sadd(registry_key, key)
        pipe.expire(registry_key, ttl)
        await pipe.execute()

def _k_stm(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:stm:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_mtm(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:mtm:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_mtm_tokens(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:mtm_tokens:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_mtm_recent(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:mtm_recent:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_mtm_recent_tokens(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:mtm_recent_tokens:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_ltm(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:ltm:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_ltm_slices(chat_id: int, user_id: int, namespace: str = "default") -> str:
    prefix = _ns_prefix(namespace)
    return f"{prefix}mem:ltm_slices:{'p' if chat_id==user_id else f'g:{chat_id}:u'}:{user_id}"

def _k_g_stm(chat_id: int) -> str:
    return f"mem:stm:g:{chat_id}:all"

def _k_g_stm_tokens(chat_id: int) -> str:
    return f"mem:stm_tokens:g:{chat_id}:all"

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

_LUA_TRIM_STM_TO_LIMIT = """
local src = KEYS[1]
local dst = KEYS[2]
local limit = tonumber(ARGV[1]) or 0
local n = redis.call('LLEN', src)
if n <= limit then return {0, 0} end
local to_move = n - limit
local moved = 0
local total_len = 0
for i=1,to_move do
  local v = redis.call('LPOP', src)
  if not v then break end
  redis.call('RPUSH', dst, v)
  moved = moved + 1
  total_len = total_len + string.len(v)
end
return {moved, total_len}
"""

_LUA_TRIM_STM_TO_TOKEN_BUDGET = """
local src = KEYS[1]
local dst = KEYS[2]
local budget = tonumber(ARGV[1]) or 0
local cpt = tonumber(ARGV[2]) or 3.8
local align_pairs = tostring(ARGV[3] or "1")
local min_keep_pairs = tonumber(ARGV[4] or 1)

local n = redis.call('LLEN', src)
if n <= 1 then return {0, 0} end

local function tok_of(s)
  return math.max(1, math.floor(string.len(s) / math.max(0.1, cpt)))
end

local acc = 0
local kept = 0
local min_keep_msgs = math.min(n, math.max(0, min_keep_pairs * 2))
local keep_from = 1

for i = n, 1, -1 do
  local v = redis.call('LINDEX', src, i - 1)
  acc = acc + tok_of(v)
  kept = kept + 1
  if acc > budget and kept >= min_keep_msgs then
    keep_from = i + 1
    break
  end
  if i == 1 then
    keep_from = 1
  end
end

local to_move = math.max(0, keep_from - 1)

if align_pairs ~= "0" and to_move > 0 then
  local remaining = n - to_move
  if (remaining % 2) ~= 0 and to_move < n then
    to_move = to_move + 1
  end
end

local moved = 0
local total_len = 0
for i = 1, to_move do
  local v = redis.call('LPOP', src)
  if not v then break end
  redis.call('RPUSH', dst, v)
  moved = moved + 1
  total_len = total_len + string.len(v)
end

return {moved, total_len}
"""

LUA_APPEND_AND_TRIM = """
local key_list = KEYS[1]
local key_tok  = KEYS[2]
local budget   = tonumber(ARGV[1])
local n        = tonumber(ARGV[2])
local total    = 0

for i=1,n do
  local s = ARGV[2+i]
  redis.call('RPUSH', key_list, s)
  total = total + string.len(s)
end

local cpt = tonumber(ARGV[3 + n]) or 3.8
local inc = math.max(1, math.floor(total / math.max(0.1, cpt)))
local cur = tonumber(redis.call('INCRBY', key_tok, inc)) or inc

local function tok_of(s)
  return math.max(1, math.floor(string.len(s)/math.max(0.1,cpt)))
end

while cur > budget do
  local v = redis.call('LPOP', key_list)
  if not v then
    cur = 0
    break
  end
  cur = math.max(0, cur - tok_of(v))
end

redis.call('SET', key_tok, cur)
return cur
"""

_LUA_TRIM_STM_TO_LIMIT_RATIO = """
local src = KEYS[1]
local dst = KEYS[2]
local limit = tonumber(ARGV[1]) or 0
local ratio = tonumber(ARGV[2]) or 0.30
local align_pairs = tostring(ARGV[3] or "1")
local min_keep_pairs = tonumber(ARGV[4] or 1)

local n = redis.call('LLEN', src)
if n <= limit then return {0, 0} end

local min_keep_msgs = math.min(n, math.max(0, min_keep_pairs * 2))
local to_move = math.floor(n * ratio + 0.00001)
if to_move < 1 then to_move = 1 end
if to_move > (n - min_keep_msgs) then
  to_move = math.max(0, n - min_keep_msgs)
end

if align_pairs ~= "0" and to_move > 0 then
  local remaining = n - to_move
  if (remaining % 2) ~= 0 and to_move < n then
    to_move = to_move + 1
  end
end

local moved = 0
local total_len = 0
for i = 1, to_move do
  local v = redis.call('LPOP', src)
  if not v then break end
  redis.call('RPUSH', dst, v)
  moved = moved + 1
  total_len = total_len + string.len(v)
end
return {moved, total_len}
"""

_LUA_TRIM_STM_TO_TOKEN_BUDGET_RATIO = """
local src = KEYS[1]
local dst = KEYS[2]
local budget = tonumber(ARGV[1]) or 0
local cpt = tonumber(ARGV[2]) or 3.8
local align_pairs = tostring(ARGV[3] or "1")
local min_keep_pairs = tonumber(ARGV[4] or 1)
local ratio = tonumber(ARGV[5]) or 0.30

local n = redis.call('LLEN', src)
if n <= 1 then return {0, 0} end

local function tok_of(s)
  return math.max(1, math.floor(string.len(s) / math.max(0.1, cpt)))
end

local acc = 0
local kept = 0
local min_keep_msgs = math.min(n, math.max(0, min_keep_pairs * 2))
local over = false
for i = n, 1, -1 do
  local v = redis.call('LINDEX', src, i - 1)
  acc = acc + tok_of(v)
  kept = kept + 1
  if acc > budget and kept >= min_keep_msgs then
    over = true
    break
  end
end
if not over then return {0, 0} end

local to_move = math.floor(n * ratio + 0.00001)
if to_move < 1 then to_move = 1 end
if to_move > (n - min_keep_msgs) then
  to_move = math.max(0, n - min_keep_msgs)
end

if align_pairs ~= "0" and to_move > 0 then
  local remaining = n - to_move
  if (remaining % 2) ~= 0 and to_move < n then
    to_move = to_move + 1
  end
end

local moved = 0
local total_len = 0
for i = 1, to_move do
  local v = redis.call('LPOP', src)
  if not v then break end
  redis.call('RPUSH', dst, v)
  moved = moved + 1
  total_len = total_len + string.len(v)
end
return {moved, total_len}
"""

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

def _fmt_date_utc(ts: int) -> str:
    try:
        if int(ts) <= 0:
            return ""
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""

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

async def is_spam(chat_id: int, user_id: int) -> bool:
    try:
        window = int(getattr(settings, "SPAM_WINDOW", 10))
    except Exception:
        window = 10
    try:
        limit = int(getattr(settings, "SPAM_LIMIT", 6))
    except Exception:
        limit = 6
    key = f"spam:{chat_id}"
    redis = get_redis()

    try:
        res = await redis.eval(
            """
local key = KEYS[1]
local field = ARGV[1]
local ttl = tonumber(ARGV[2])
local count = redis.call('HINCRBY', key, field, 1)
redis.call('EXPIRE', key, ttl)
return count
""",
            1,
            key,
            str(user_id),
            max(1, window + 2),
        )
        count = int(res or 0)
        return count > limit
    except Exception:
        logger.exception("is_spam error for chat %s (fallback to local counter)", chat_id)
        k = (chat_id, user_id)
        local_ttl = max(1, window * 2)
        try:
            local_max_size = int(getattr(settings, "LOCAL_SPAM_MAX_SIZE", 5000))
        except Exception:
            local_max_size = 5000
        # Fallback-only counter; consider moving TTL/size limits to settings (e.g. LOCAL_SPAM_MAX_SIZE).
        async with _get_local_spam_lock_for_current_loop():
            with _local_spam_guard:
                now = time_module.time()
                c, ts = _local_spam.get(k, (0, now))
                if now - ts > window:
                    c, ts = 0, now
                c += 1
                _local_spam[k] = (c, ts)
                _local_spam_order.append((ts, k))
                while _local_spam_order:
                    oldest_ts, oldest_key = _local_spam_order[0]
                    if now - oldest_ts <= local_ttl:
                        break
                    _local_spam_order.popleft()
                    current = _local_spam.get(oldest_key)
                    if current is not None and current[1] == oldest_ts:
                        _local_spam.pop(oldest_key, None)
                if local_max_size > 0:
                    while len(_local_spam) > local_max_size and _local_spam_order:
                        oldest_ts, oldest_key = _local_spam_order.popleft()
                        current = _local_spam.get(oldest_key)
                        if current is not None and current[1] == oldest_ts:
                            _local_spam.pop(oldest_key, None)
        return c > limit

async def inc_msg_count(chat_id: int) -> None:
    redis = get_redis()
    try:
        async with redis.pipeline(transaction=False) as p:
            p.incr(f"msg_count:{chat_id}")
            p.expire(f"msg_count:{chat_id}", 86_400)
            await p.execute()
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
            pipe.expire(
                f"last_message_ts:{chat_id}",
                int(getattr(settings, "GROUP_PING_ACTIVE_TTL_SECONDS", 3600))
            )
            pipe.set(f"last_user_ts:{user_id}", now)
            pipe.expire(f"last_user_ts:{user_id}", MEMORY_TTL)
            if chat_id == user_id:
                pipe.set(f"last_private_ts:{user_id}", now)
                pipe.expire(f"last_private_ts:{user_id}", MEMORY_TTL)
            await pipe.execute()
    except Exception:
        logger.exception("record_activity error chat=%s user=%s", chat_id, user_id)
        return
    try:
        await _register_user_key(redis, user_id, f"last_user_ts:{user_id}", USER_KEYS_REGISTRY_TTL)
        if chat_id == user_id:
            await _register_user_key(redis, user_id, f"last_private_ts:{user_id}", USER_KEYS_REGISTRY_TTL)
    except Exception:
        logger.debug("record_activity: register user ts failed chat=%s user=%s", chat_id, user_id, exc_info=True)

async def get_cached_gender(user_id: int) -> Optional[str]:
    redis = get_redis()
    raw = await redis.hgetall(f"user_gender_counts:{user_id}") or {}
    counts: Dict[str, int] = {}
    items = raw.items() if isinstance(raw, dict) else []
    for g, c in items:
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
        return
    try:
        await _register_user_key(redis, user_id, f"user_gender_counts:{user_id}", USER_KEYS_REGISTRY_TTL)
    except Exception:
        logger.debug("cache_gender: register key failed user=%s", user_id, exc_info=True)

async def push_message(
    chat_id: int,
    role: str,
    content: str,
    *,
    user_id: int,
    speaker_id: int | None = None,
    namespace: str = "default",
) -> None:
    r = (role or "").strip().lower()
    if r not in ("user", "assistant", "system"):
        r = "user"
    entry: Dict[str, Any] = {
        "role": r,
        "content": content,
        "chat_id": chat_id,
        "user_id": user_id,
        "ts": time_module.time(),
    }
    if speaker_id is not None:
        try:
            entry["speaker_id"] = int(speaker_id)
        except Exception:
            pass

    redis = get_redis()
    try:
        key_stm = _k_stm(chat_id, user_id, namespace)
        async with redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key_stm, json.dumps(entry, ensure_ascii=False))
            pipe.expire(key_stm, MEMORY_TTL_STM_MTM)
            await pipe.execute()
    except Exception:
        logger.exception("push_message STM write error for chat %s", chat_id)
        return
    try:
        await _register_user_key(redis, user_id, key_stm, USER_KEYS_REGISTRY_TTL)
    except Exception:
        logger.debug("push_message: register STM key failed chat=%s user=%s", chat_id, user_id, exc_info=True)

    try:
        asyncio.create_task(_enforce_stm_and_promote_to_mtm(chat_id, user_id, namespace=namespace))
    except Exception:
        logger.exception("STM→MTM promote failed chat=%s user=%s", chat_id, user_id)

    try:
        if namespace == "api":
            return

        if chat_id == user_id:

            async def _display_name(uid: int) -> str:
                ui_raw = await get_redis().hgetall(f"tg_user:{uid}") or {}
                ui = { _b2s(k): _b2s(v) for k, v in ui_raw.items() }
                first = ui.get("first_name") or ""
                last  = ui.get("last_name") or ""
                uname = ui.get("username") or ""
                name = (f"{first} {last}".strip()) or (f"@{uname}" if uname else "")
                return name or str(uid)
            
            if not settings.ENABLE_DIALOG_LOGGING:
                return
            if r == "user":
                await log_user_message(user_id, await _display_name(user_id), content)
            elif r == "assistant":
                bot_name = getattr(settings, "BOT_NAME", "BOT")
                await log_bot_message(user_id, bot_name, content)
    except Exception:
        logger.exception("dialog logging failed chat=%s user=%s", chat_id, user_id)

async def _enforce_stm_and_promote_to_mtm(chat_id: int, user_id: int, namespace: str = "default") -> None:
    if not getattr(settings, "LAYERED_MEMORY_ENABLED", True):
        return

    redis = get_redis()

    try:
        guard_prefix = _ns_prefix(namespace)
        guard_key = f"{guard_prefix}stm_promote_guard:{chat_id}:{user_id}"
        if not await redis.set(guard_key, 1, nx=True, ex=int(getattr(settings, "STM_PROMOTE_GUARD_EX", 5))):
            return
    except Exception:
        pass

    key_stm = _k_stm(chat_id, user_id, namespace)
    key_mtm = _k_mtm(chat_id, user_id, namespace)
    is_private = (chat_id == user_id)

    try:
        stm_budget_tokens = int(getattr(settings, "STM_TOKEN_BUDGET", 0) or 0)
    except Exception:
        stm_budget_tokens = 0

    moved_meta = [0, 0, []]

    if stm_budget_tokens > 0:
        try:
            cpt = float(getattr(settings, "APPROX_CHARS_PER_TOKEN", 3.8))
        except Exception:
            cpt = 3.8
        try:
            pair_align = "1" if bool(getattr(settings, "STM_PAIR_ALIGN", True)) else "0"
        except Exception:
            pair_align = "1"
        try:
            min_keep_pairs = int(getattr(settings, "STM_MIN_KEEP_PAIRS", 1))
        except Exception:
            min_keep_pairs = 1

        ratio = str(float(getattr(settings, "STM_TRIM_RATIO", 0.25)))

        try:
            moved_meta = await redis.eval(
                _LUA_TRIM_STM_TO_TOKEN_BUDGET_RATIO,
                2, key_stm, key_mtm,
                str(int(stm_budget_tokens)),
                str(cpt),
                pair_align,
                str(int(min_keep_pairs)),
                ratio
            )
        except Exception:
            logger.exception("stm->mtm ratio trim (token) failed — falling back to strict budget trim chat=%s user=%s", chat_id, user_id)
            try:
                moved_meta = await redis.eval(
                    _LUA_TRIM_STM_TO_TOKEN_BUDGET,
                    2, key_stm, key_mtm,
                    str(int(stm_budget_tokens)),
                    str(cpt),
                    pair_align,
                    str(int(min_keep_pairs)),
                )
            except Exception:
                logger.exception("stm->mtm fallback trim-to-token-budget failed chat=%s user=%s", chat_id, user_id)
                moved_meta = [0, 0, []]
    else:
        pair_limit = (
            int(getattr(settings, "STM_PAIR_LIMIT_PRIVATE", 50))
            if is_private
            else int(getattr(settings, "STM_PAIR_LIMIT_GROUP", 12))
        )
        msg_limit = max(2, int(pair_limit) * 2)

        try:
            pair_align = "1" if bool(getattr(settings, "STM_PAIR_ALIGN", True)) else "0"
        except Exception:
            pair_align = "1"
        try:
            min_keep_pairs = int(getattr(settings, "STM_MIN_KEEP_PAIRS", 1))
        except Exception:
            min_keep_pairs = 1
        ratio = str(float(getattr(settings, "STM_TRIM_RATIO", 0.25)))

        try:
            moved_meta = await redis.eval(
                _LUA_TRIM_STM_TO_LIMIT_RATIO,
                2, key_stm, key_mtm,
                str(msg_limit),
                ratio,
                pair_align,
                str(int(min_keep_pairs))
            )
        except Exception:
            logger.exception("stm->mtm ratio trim (count) failed — falling back to strict limit trim chat=%s user=%s", chat_id, user_id)
            try:
                moved_meta = await redis.eval(_LUA_TRIM_STM_TO_LIMIT, 2, key_stm, key_mtm, str(msg_limit))
            except Exception:
                logger.exception("stm->mtm fallback trim-to-limit failed chat=%s user=%s", chat_id, user_id)
                moved_meta = [0, 0, []]

    local_moved = int(moved_meta[0] or 0)
    if local_moved <= 0:
        return

    try:
        await redis.expire(key_mtm, MEMORY_TTL_STM_MTM)
        await _register_user_key(redis, user_id, key_mtm, USER_KEYS_REGISTRY_TTL)
    except Exception:
        pass

    try:
        cpt = float(getattr(settings, "APPROX_CHARS_PER_TOKEN", 3.8))
    except Exception:
        cpt = 3.8
    try:
        moved_bytes = int(moved_meta[1] or 0)
        delta = max(1, int(moved_bytes / max(0.1, cpt)))
        key_tok = _k_mtm_tokens(chat_id, user_id, namespace)
        await redis.incrby(key_tok, int(delta))
        await redis.expire(key_tok, MEMORY_TTL_STM_MTM)
        await _register_user_key(redis, user_id, key_tok, USER_KEYS_REGISTRY_TTL)
    except Exception:
        pass

    try:
        window_budget = int(getattr(settings, "MTM_RECENT_WINDOW_TOKENS", 30_000))
    except Exception:
        window_budget = 30_000
    if window_budget > 0:
        try:
            key_win = _k_mtm_recent(chat_id, user_id, namespace)
            key_win_tok = _k_mtm_recent_tokens(chat_id, user_id, namespace)
            moved_tail = await redis.lrange(key_mtm, -local_moved, -1) if local_moved > 0 else []
            buf = []
            for r in moved_tail or []:
                try:
                    s = _b2s(r)
                    obj = json.loads(s)
                    role = (obj.get("role") or "").strip()
                    if role not in ("user", "assistant"):
                        continue
                    ts = int(obj.get("ts") or 0)
                    content = (obj.get("content") or "").strip()
                    if content:
                        buf.append(f"[{ts}] {role}: {content}")
                except Exception:
                    continue
            if buf:
                await redis.eval(
                    LUA_APPEND_AND_TRIM, 2, key_win, key_win_tok,
                    str(window_budget), str(len(buf)), *buf,
                    str(getattr(settings, "APPROX_CHARS_PER_TOKEN", 3.8))
                )
                try:
                    await redis.expire(key_win, MEMORY_TTL_STM_MTM)
                    await redis.expire(key_win_tok, MEMORY_TTL_STM_MTM)
                except Exception:
                    pass
                try:
                    await _register_user_key(redis, user_id, key_win, USER_KEYS_REGISTRY_TTL)
                    await _register_user_key(redis, user_id, key_win_tok, USER_KEYS_REGISTRY_TTL)
                except Exception:
                    pass
        except Exception:
            logger.exception("update MTM recent window failed chat=%s user=%s", chat_id, user_id)

    await _maybe_schedule_ltm_rollup(chat_id, user_id, namespace=namespace)

async def _maybe_schedule_ltm_rollup(chat_id: int, user_id: int, namespace: str = "default") -> None:
    if not getattr(settings, "LAYERED_MEMORY_ENABLED", True):
        return

    redis = get_redis()
    is_private = (chat_id == user_id)
    budget = (
        settings.MTM_BUDGET_TOKENS_PRIVATE if is_private else settings.MTM_BUDGET_TOKENS_GROUP
    )
    try:
        cur = int(await redis.get(_k_mtm_tokens(chat_id, user_id, namespace)) or 0)
    except Exception:
        cur = 0

    budget_reached = (cur >= int(budget))
    if not budget_reached:
        return

    guard_prefix = _ns_prefix(namespace)
    guard = f"{guard_prefix}ltm_rollup_guard:{chat_id}:{user_id}"
    try:
        ex_guard = int(getattr(settings, "LTM_ROLLUP_GUARD_EX_SEC", 240))
        if not await redis.set(guard, 1, nx=True, ex=ex_guard):
            return
    except Exception:
        pass

    try:
        from app.tasks.celery_app import celery
        ns = namespace or "default"
        if is_private:
            celery.send_task("ltm.rollup_private", args=[user_id, ns])
        else:
            celery.send_task("ltm.rollup_group", args=[chat_id, user_id, ns])
    except Exception:
        logger.exception("Failed to enqueue LTM rollup chat=%s user=%s", chat_id, user_id)

async def load_context(
    chat_id: int,
    user_id: int,
    namespace: str = "default",
) -> List[Dict[str, Any]]:

    redis = get_redis()
    key_stm = _k_stm(chat_id, user_id, namespace)

    try:
        rows = await redis.lrange(key_stm, 0, -1)
    except Exception:
        logger.exception("load_context STM error chat=%s user=%s", chat_id, user_id)
        return []

    ctx: List[Dict[str, Any]] = []
    for r in rows or []:
        try:
            s = _b2s(r)
            ctx.append(json.loads(s))
        except json.JSONDecodeError:
            s = _b2s(r)
            logger.warning("Bad JSON in STM chat=%s (len=%d, preview=%r)", chat_id, len(s or ""), (s or "")[:120])

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

async def get_ltm_text(chat_id: int, user_id: int, namespace: str = "default") -> str:
    raw = await get_redis().get(_k_ltm(chat_id, user_id, namespace))
    return extract_summary_data(raw) if raw else ""

async def push_ltm_slice(
    chat_id: int,
    user_id: int,
    text: str,
    cap_items: int = 120,
    ttl_override: Optional[int] = None,
    namespace: str = "default",
) -> None:

    if not (text or "").strip():
        return
    redis = get_redis()
    key = _k_ltm_slices(chat_id, user_id, namespace)
    payload = json.dumps({"ts": time_module.time(), "text": text}, ensure_ascii=False)
    async with redis.pipeline(transaction=True) as p:
        p.rpush(key, payload)
        p.ltrim(key, -cap_items, -1)
        try:
            ttl = int(ttl_override) if ttl_override is not None else int(MEMORY_TTL_LTM)
        except Exception:
            ttl = MEMORY_TTL_LTM
        p.expire(key, ttl)
        await p.execute()
    try:
        await _register_user_key(redis, user_id, key, USER_KEYS_REGISTRY_TTL)
    except Exception:
        logger.debug("push_ltm_slice: register key failed chat=%s user=%s", chat_id, user_id, exc_info=True)

async def get_ltm_slices(
    chat_id: int,
    user_id: int,
    cap_items: int = 40,
    namespace: str = "default",
) -> List[str]:

    redis = get_redis()
    start = -int(cap_items) if int(cap_items) > 0 else 0
    rows = await redis.lrange(_k_ltm_slices(chat_id, user_id, namespace), start, -1)
    out: List[str] = []
    for r in rows or []:
        s = _b2s(r)
        try:
            obj = json.loads(s)
            txt = (obj.get("text") or "").strip()
            ts  = int(obj.get("ts") or 0)
        except Exception:
            txt = s.strip()
            ts  = 0
        if not txt:
            continue
        date = _fmt_date_utc(ts)
        out.append(f"[{date}] {txt}" if date else txt)
    return out

async def get_all_mtm_texts(
    chat_id: int,
    user_id: int,
    cap_tokens: int = 120_000,
    namespace: str = "default",
) -> List[str]:

    redis = get_redis()
    key = _k_mtm(chat_id, user_id, namespace)

    try:
        n = int(await redis.llen(key) or 0)
    except Exception:
        return []

    if n <= 0:
        return []

    if cap_tokens is None or int(cap_tokens) <= 0:
        rows = await redis.lrange(key, 0, -1)
        out: List[str] = []
        for r in rows or []:
            try:
                s = _b2s(r)
                obj = json.loads(s)
                ts = int(obj.get("ts") or 0)
                role = (obj.get("role") or "").strip()
                content = (obj.get("content") or "").strip()
                if ts > 0:
                    txt = f"[{ts}] {role}: {content}" if content else f"[{ts}] {role}"
                else:
                    txt = f"{role}: {content}" if (role or content) else s
            except Exception:
                txt = _b2s(r)
            out.append(txt)
        return out

    cap = max(1, int(cap_tokens))
    out_rev: List[str] = []
    acc = 0
    CHUNK = 500
    end = n - 1
    while end >= 0 and acc < cap:
        start = max(0, end - CHUNK + 1)
        rows = await redis.lrange(key, start, end)
        if not rows:
            break
        for r in reversed(rows):
            try:
                s = _b2s(r)
                obj = json.loads(s)
                ts = int(obj.get("ts") or 0)
                role = (obj.get("role") or "").strip()
                content = (obj.get("content") or "").strip()
                if ts > 0:
                    txt = f"[{ts}] {role}: {content}" if content else f"[{ts}] {role}"
                else:
                    txt = f"{role}: {content}" if (role or content) else s
            except Exception:
                txt = _b2s(r)
            acc += approx_tokens(txt)
            out_rev.append(txt)
            if acc >= cap:
                break
        end = start - 1
    return list(reversed(out_rev))

async def last_activity_ts(user_id: int) -> float:

    r = get_redis()
    best = 0.0

    try:
        raw = await r.get(f"last_user_ts:{user_id}")
        if raw:
            try:
                best = max(best, float(_b2s(raw)))
            except Exception:
                pass
    except Exception:
        pass

    try:
        raw = await r.get(f"last_private_ts:{user_id}")
        if raw:
            try:
                best = max(best, float(_b2s(raw)))
            except Exception:
                pass
    except Exception:
        pass

    try:
        raw = await r.get(f"last_message_ts:{user_id}")
        if raw:
            try:
                best = max(best, float(_b2s(raw)))
            except Exception:
                pass
    except Exception:
        pass

    try:
        score = await r.zscore(f"user_last_ts:{user_id}", str(user_id))
        if score:
            best = max(best, float(score))
    except Exception:
        pass

    try:
        lp = await r.hgetall(f"last_ping:pm:{user_id}") or {}
        raw_ts = lp.get("ts") if "ts" in lp else lp.get(b"ts")
        if raw_ts:
            best = max(best, float(_b2s(raw_ts)))
    except Exception:
        pass
    return best

async def is_recently_active(user_id: int, within_days: int = 14) -> bool:

    try:
        horizon = max(1, int(within_days)) * 86_400
    except Exception:
        horizon = 14 * 86_400
    ts = await last_activity_ts(int(user_id))
    return ts > 0 and (time_module.time() - ts) < horizon


async def register_api_memory_uid(api_key_id: int, memory_uid: int) -> None:

    try:
        api_key_id = int(api_key_id)
        memory_uid = int(memory_uid)
    except Exception:
        return
    if api_key_id <= 0 or memory_uid <= 0:
        return

    r = get_redis()
    try:
        key = f"api:uidset:{api_key_id}"
        await r.sadd(key, memory_uid)
        ttl_conf = int(getattr(settings, "API_UIDSET_TTL_SEC", MEMORY_TTL_LTM))
        await r.expire(key, max(MEMORY_TTL_LTM, ttl_conf))
    except Exception:
        logger.exception("register_api_memory_uid failed api_key_id=%s", api_key_id)


async def delete_user_redis_data(user_id: int) -> int:

    redis = get_redis()
    vredis = get_redis_vector()

    async def _iter_keys(match_pat: str):
        async for key in redis.scan_iter(match=match_pat, count=SCAN_COUNT):
            yield key

    async def _unlink_batch(keys: list) -> int:
        if not keys:
            return 0
        try:
            return int(await redis.unlink(*keys))
        except Exception:
            return int(await redis.delete(*keys))

    async def _iter_keys_on(r, match_pat: str):
        async for key in r.scan_iter(match=match_pat, count=SCAN_COUNT):
            yield key

    async def _unlink_batch_on(r, keys: list) -> int:
        if not keys:
            return 0
        try:
            return int(await r.unlink(*keys))
        except Exception:
            return int(await r.delete(*keys))

    async def _cleanup_memory_uid_zset(zname: str) -> None:
        zname = _b2s(zname, str(zname))
        parts = zname.split(":")
        chat = parts[2] if len(parts) >= 4 else None
        if not chat:
            return
        eids = await redis.zrange(zname, 0, -1)
        if eids:
            async with redis.pipeline(transaction=True) as p:
                for eid in eids:
                    s = _b2s(eid, str(eid))
                    p.delete(f"memory:{chat}:{s}")
                    p.zrem(f"memory:ids:{chat}", s)
                p.delete(zname)
                p.srem(f"memory:uidsets:{chat}", zname)
                await p.execute()

    async def _cleanup_memtxt_uid_zset(zname: str) -> None:
        zname = _b2s(zname, str(zname))
        parts = zname.split(":")
        chat = parts[2] if len(parts) >= 4 else None
        if not chat:
            return
        doc_keys = await redis.zrange(zname, 0, -1)
        if doc_keys:
            async with redis.pipeline(transaction=True) as p:
                for dk in doc_keys:
                    k = _b2s(dk, str(dk))
                    p.delete(k)
                    p.zrem(f"memtxt:ids:{chat}", k)
                p.delete(zname)
                await p.execute()

    deleted_keys = 0
    registry_deleted = 0

    # Per-user keys are removed through the registry; shared indexes are cleaned separately.
    for registry_key in (_user_keys_registry(user_id), _user_keys_registry(user_id, "api")):
        try:
            raw_keys = await redis.smembers(registry_key)
        except Exception:
            raw_keys = []
        if not raw_keys:
            try:
                await redis.delete(registry_key)
            except Exception:
                pass
            continue
        registry_deleted += len(raw_keys)
        keys_to_delete: list[str] = []
        memtxt_seen_keys: list[str] = []
        memory_uid_zsets: list[str] = []
        memtxt_uid_zsets: list[str] = []
        for raw_key in raw_keys:
            key = _b2s(raw_key, str(raw_key))
            if key.startswith("memory:ids:") and key.endswith(f":{user_id}"):
                memory_uid_zsets.append(key)
                continue
            if key.startswith("memtxt:ids:") and key.endswith(f":{user_id}"):
                memtxt_uid_zsets.append(key)
                continue
            if key.startswith("memtxt:seen:") and key.endswith(f":{user_id}"):
                memtxt_seen_keys.append(key)
                continue
            keys_to_delete.append(key)

        for zname in memory_uid_zsets:
            await _cleanup_memory_uid_zset(zname)
        for zname in memtxt_uid_zsets:
            await _cleanup_memtxt_uid_zset(zname)
        keys_to_delete.extend(memtxt_seen_keys)

        batch: list = []
        for k in keys_to_delete:
            batch.append(k)
            if len(batch) >= SCAN_COUNT:
                deleted_keys += await _unlink_batch(batch)
                batch.clear()
        if batch:
            deleted_keys += await _unlink_batch(batch)
            batch.clear()

        try:
            await redis.delete(registry_key)
        except Exception:
            pass

    direct_keys = [
        f"personal_enrolled:{user_id}",
        f"private_idle_list:{user_id}",
        f"personal_ping_streak:{user_id}",
        f"pending_ping:{user_id}",
        f"ping_arm_stats:{user_id}",
        f"private_hod_hist:{user_id}",
        f"personal_reanimate_last_ts:{user_id}",
        f"last_ping:pm:{user_id}",
        f"msg_count:{user_id}",
        f"last_message_ts:{user_id}",
        f"lang:{user_id}",
        f"lang_ui:{user_id}",
        f"persona:wizard:{user_id}",
        f"pending_invoice:{user_id}",
        f"pending_invoice_tier:{user_id}",
        f"pending_invoice_msg:{user_id}",
        f"buy_menu_msg:{user_id}",
        f"buy_info_msg:{user_id}",
        f"cb_rate:{user_id}",
        f"tts:pref:{user_id}",
        f"vmsg:disabled:chat:{user_id}",
    ]
    deleted_keys += await _unlink_batch(direct_keys)

    batch = []
    for pat in (f"msg:{user_id}:*", f"seen:{user_id}:*", f"on_topic_daily:{user_id}:*"):
        async for k in _iter_keys(pat):
            batch.append(k)
            if len(batch) >= SCAN_COUNT:
                deleted_keys += await _unlink_batch(batch)
                batch.clear()
        if batch:
            deleted_keys += await _unlink_batch(batch)
            batch.clear()

    srem_batch = []
    async for key in _iter_keys("all_users:*"):
        srem_batch.append(key)
        if len(srem_batch) >= SCAN_COUNT:
            async with redis.pipeline(transaction=False) as p:
                for k in srem_batch:
                    p.srem(k, str(user_id))
                await p.execute()
            srem_batch.clear()
    if srem_batch:
        async with redis.pipeline(transaction=False) as p:
            for k in srem_batch:
                p.srem(k, str(user_id))
            await p.execute()

    zrem_batch = []
    async for key in _iter_keys("user_last_ts:*"):
        zrem_batch.append(key)
        if len(zrem_batch) >= SCAN_COUNT:
            async with redis.pipeline(transaction=False) as p:
                for k in zrem_batch:
                    p.zrem(k, user_id)
                await p.execute()
            zrem_batch.clear()
    if zrem_batch:
        async with redis.pipeline(transaction=False) as p:
            for k in zrem_batch:
                p.zrem(k, user_id)
            await p.execute()

    hdel_batch = []
    uid_field = str(user_id)
    async for key in _iter_keys("spam:*"):
        hdel_batch.append(key)
        if len(hdel_batch) >= SCAN_COUNT:
            async with redis.pipeline(transaction=False) as p:
                for k in hdel_batch:
                    p.hdel(k, uid_field)
                await p.execute()
            hdel_batch.clear()
    if hdel_batch:
        async with redis.pipeline(transaction=False) as p:
            for k in hdel_batch:
                p.hdel(k, uid_field)
            await p.execute()

    batch = []
    for pat in (f"facts:{user_id}:*", f"plans:{user_id}:*", f"bounds:{user_id}:*"):
        async for k in _iter_keys(pat):
            batch.append(k)
            if len(batch) >= SCAN_COUNT:
                deleted_keys += await _unlink_batch(batch)
                batch.clear()
        if batch:
            deleted_keys += await _unlink_batch(batch)
            batch.clear()

    try:
        for pat in (f"facts:{user_id}:*", f"plans:{user_id}:*", f"bounds:{user_id}:*"):
            vec_batch: list = []
            async for k in _iter_keys_on(vredis, pat):
                vec_batch.append(k)
                if len(vec_batch) >= SCAN_COUNT:
                    deleted_keys += await _unlink_batch_on(vredis, vec_batch)
                    vec_batch.clear()
            if vec_batch:
                deleted_keys += await _unlink_batch_on(vredis, vec_batch)
                vec_batch.clear()
    except Exception:
        logger.debug("vector-redis LTM key cleanup failed", exc_info=True)

    for r in (redis, vredis):
        try:
            await r.zrem("ltm:last_active", str(user_id))
        except Exception:
            pass

    try:
        z_main = f"memory:ids:{user_id}"
        eids = await redis.zrange(z_main, 0, -1)
        if eids:
            async with redis.pipeline(transaction=True) as p:
                for eid in eids:
                    s = _b2s(eid, str(eid))
                    p.delete(f"memory:{user_id}:{s}")
                    p.zrem(z_main, s)
                await p.execute()
        try:
            await redis.delete(z_main)
        except Exception:
            pass
        try:
            await redis.srem(f"memory:uidsets:{user_id}", f"memory:ids:{user_id}:{user_id}")
            await redis.delete(f"memory:ids:{user_id}:{user_id}")
        except Exception:
            pass
    except Exception:
        logger.debug("private vector memory cleanup failed", exc_info=True)

    for pat in (f"memtxt:ids:{user_id}", f"memtxt:ids:{user_id}:{user_id}",
                f"memtxt:seen:{user_id}", f"memtxt:seen:{user_id}:{user_id}"):
        try:
            await redis.delete(pat)
        except Exception:
            pass

    logger.info(
        "delete_user_redis_data: user_id=%s, deleted_keys=%s, registry_deleted=%s (extended)",
        user_id,
        deleted_keys,
        registry_deleted,
    )
    return deleted_keys


async def cleanup_api_key_memory(api_key_id: int) -> int:

    try:
        api_key_id = int(api_key_id)
    except Exception:
        return 0
    if api_key_id <= 0:
        return 0

    r = get_redis()
    uidset_key = f"api:uidset:{api_key_id}"

    try:
        uids = await r.smembers(uidset_key)
    except Exception:
        uids = []

    if not uids:
        try:
            await r.delete(uidset_key)
        except Exception:
            pass
        return 0

    total_deleted = 0
    for raw in uids:
        try:
            uid = int(_b2s(raw))
        except Exception:
            continue
        try:
            total_deleted += await delete_user_redis_data(uid)
        except Exception:
            logger.exception(
                "cleanup_api_key_memory: delete_user_redis_data failed for api_key_id=%s uid=%s",
                api_key_id,
                uid,
            )

    try:
        await r.delete(uidset_key)
    except Exception:
        pass

    logger.info(
        "cleanup_api_key_memory: api_key_id=%s, memory_uids=%s, total_deleted_keys=%s",
        api_key_id,
        len(uids),
        total_deleted,
    )
    return total_deleted


async def push_group_stm(chat_id: int, role: str, content: str, *, user_id: int) -> None:

    txt = (content or "").strip()
    if not txt:
        return
    entry = {
        "role": (role or "user"),
        "content": txt,
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "ts": time_module.time(),
    }
    payload = json.dumps(entry, ensure_ascii=False)
    r = get_redis()
    key_list = _k_g_stm(chat_id)
    key_tok  = _k_g_stm_tokens(chat_id)
    try:
        budget = settings.GROUP_STM_TOKEN_BUDGET
    except Exception:
        budget = 6000
    try:
        cpt = settings.APPROX_CHARS_PER_TOKEN
    except Exception:
        cpt = 3.8
    try:
        await r.eval(LUA_APPEND_AND_TRIM, 2, key_list, key_tok, str(budget), "1", payload, str(cpt))
        await r.expire(key_list, MEMORY_TTL_STM_MTM)
        await r.expire(key_tok,  MEMORY_TTL_STM_MTM)
    except Exception:
        logger.debug("push_group_stm failed chat=%s", chat_id, exc_info=True)

async def get_group_stm_tail(chat_id: int, cap_tokens: int = 1200, max_lines: int = 60) -> list[str]:

    r = get_redis()
    key = _k_g_stm(chat_id)
    n = int(await r.llen(key) or 0)
    if n <= 0:
        return []

    out_rev: list[str] = []
    acc = 0
    total = 0
    CHUNK = 200
    end = n - 1

    while end >= 0 and acc < max(1, int(cap_tokens)) and total < max(1, int(max_lines)):
        start = max(0, end - CHUNK + 1)
        rows = await r.lrange(key, start, end)
        if not rows:
            break
        for raw in reversed(rows):
            try:
                s = _b2s(raw)
                obj = json.loads(s)
                role = (obj.get("role") or "user")
                ts = float(obj.get("ts") or 0)
                uid = obj.get("user_id")
                txt = (obj.get("content") or "").strip()
                if not txt:
                    continue
                line = f"[{int(ts)}] ({role}) [u:{uid}] {txt}"
            except Exception:
                line = _b2s(raw)
            acc += approx_tokens(line)
            total += 1
            out_rev.append(line)
            if acc >= max(1, int(cap_tokens)) or total >= max(1, int(max_lines)):
                break
        end = start - 1
    return list(reversed(out_rev))

async def append_group_recent(chat_id: int, items: list[str], budget_tokens: int | None = None) -> None:

    if not items:
        return
    r = get_redis()
    try:
        budget = int(budget_tokens) if budget_tokens is not None else int(getattr(settings, "GROUP_RECENT_TOKENS_BUDGET", 20000))
    except Exception:
        budget = 20000
    key_list = f"mem:mtm_recent:g:{chat_id}"
    key_tok  = f"mem:mtm_recent_tokens:g:{chat_id}"
    try:
        cpt = float(getattr(settings, "APPROX_CHARS_PER_TOKEN", 3.8))
    except Exception:
        cpt = 3.8
    try:
        n = len(items)
        await r.eval(LUA_APPEND_AND_TRIM, 2, key_list, key_tok, str(budget), str(n), *items, str(cpt))
        await r.expire(key_list, MEMORY_TTL_STM_MTM)
        await r.expire(key_tok,  MEMORY_TTL_STM_MTM)
    except Exception:
        logger.debug("append_group_recent failed chat=%s", chat_id, exc_info=True)
