cat > app/core/memory.py << 'EOF'
#app/core/memory.py
from __future__ import annotations

import contextvars
import json
import logging
import asyncio
import time as time_module

from typing import Any, Dict, List, Optional
from redis.asyncio import Redis, from_url

from app.config import settings

logger = logging.getLogger(__name__)

_redis_ctx: contextvars.ContextVar[dict[int, Redis]] = contextvars.ContextVar("redis_client_map")

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

        class SafePipeline:  # proxy
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

def _create_single() -> Redis:
    return from_url(
        settings.REDIS_URL,
        decode_responses=True,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        health_check_interval=30,
    )

def get_redis() -> Redis:

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop_policy().get_event_loop()

    loop_id = id(loop)

    try:
        mapping = _redis_ctx.get()
    except LookupError:
        mapping = {}
    client = mapping.get(loop_id)

    if client is None:
        raw = _create_single()
        client = SafeRedis(raw)
        mapping[loop_id] = client
        _redis_ctx.set(mapping)

    return client

async def close_redis_pools() -> None:

    mapping: dict[int, Redis] = _redis_ctx.get({})
    for client in mapping.values():
        raw: Redis = getattr(client, "_client", client)
        try:
            await raw.close()
            raw.connection_pool.disconnect(inuse_connections=True)
        except Exception as exc:
            logger.warning("Failed to close Redis pool: %s", exc)
        except RuntimeError:
            pass


def _k_g_msgs(chat_id: int, user_id: int) -> str:
    return f"mem:g:{chat_id}:u:{user_id}:msgs"


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
            _, _, length = await pipe.execute()
    except Exception:
        logger.exception("push_message error for chat %s", chat_id)
        return

    if length >= SHORT_LIMIT:
        flag = f"{key_log}:_summary_pending"
        try:
            if await redis.setnx(flag, 1):
                await redis.expire(flag, settings.SHORT_MEMORY_LIMIT * 2)
                from app.tasks.celery_app import celery
                if is_private:
                    celery.send_task("summarize_private_old", args=[user_id, length])
                else:
                    celery.send_task("summarize_group_old", args=[chat_id, user_id, length])
        except Exception:
            logger.exception("Failed to enqueue summarize task or set guard flag")

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
        ctx.append({"role": "system", "content": f"Summary: {summary}"})

    for r in rows or []:
        try:
            ctx.append(json.loads(r))
        except json.JSONDecodeError:
            logger.warning("Bad JSON in memory chat=%s: %s", chat_id, r)

    return ctx


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
    counts = {g: int(c) for g, c in raw.items() if g in ("male", "female")}
    return max(counts, key=counts.get) if counts else None


async def cache_gender(user_id: int, value: str) -> None:
    if value not in ("male", "female", "unknown"):
        return
    redis = get_redis()
    try:
        await redis.hincrby(f"user_gender_counts:{user_id}", value, 1)
        await redis.expire(f"user_gender_counts:{user_id}", MEMORY_TTL)
    except Exception:
        logger.exception("cache_gender failed user=%s", user_id)
EOF