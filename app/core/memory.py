cat > app/core/memory.py << EOF
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
                except Exception as exc:
                    if n == self._attempts - 1:
                        logger.exception(
                            "Redis cmd %s failed after %d attempts: %s",
                            name, self._attempts, exc,
                        )
                        return None
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
                            return None
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
            raw.connection_pool.disconnect()
        except Exception as exc:
            logger.warning("Failed to close Redis pool: %s", exc)


SHORT_LIMIT: int = settings.SHORT_MEMORY_LIMIT
MEMORY_TTL: int = settings.MEMORY_TTL_DAYS * 86_400

def _key_msgs(chat_id: int) -> str:
    return f"mem:msgs:{chat_id}"

def _key_summary(chat_id: int) -> str:
    return f"mem:summary:{chat_id}"

def _key_user_last_ts(chat_id: int) -> str:
    return f"user_last_ts:{chat_id}"

def _key_last_message_ts(chat_id: int) -> str:
    return f"last_message_ts:{chat_id}"

async def is_spam(chat_id: int, user_id: int) -> bool:
    window = settings.SPAM_WINDOW
    limit = settings.SPAM_LIMIT
    now = time_module.time()
    key = f"spam:{chat_id}"
    member = str(user_id)

    redis = get_redis()
    try:
        async with redis.pipeline() as pipe:
            pipe.zincrby(key, 1, member)
            pipe.zscore(key, member)
            pipe.expire(key, window)
            _, count, _ = await pipe.execute()
    except Exception:
        logger.exception("is_spam error for chat %s", chat_id)
        return False

    return int(count or 0) > limit

async def push_message(
    chat_id: int,
    role: str,
    content: str,
    user_id: Optional[int] = None,
) -> None:
    key = _key_msgs(chat_id)
    entry: Dict[str, Any] = {"role": role, "content": content}
    if user_id is not None:
        entry["user_id"] = user_id
    data = json.dumps(entry)

    redis = get_redis()
    try:
        pipe = redis.pipeline()
        pipe.rpush(key, data)
        pipe.ltrim(key, -SHORT_LIMIT, -1)
        pipe.expire(key, MEMORY_TTL)
        pipe.llen(key)
        result = await pipe.execute()
        if result is None:
            return
        length = result[-1]
    except Exception:
        logger.exception("push_message error for chat %s", chat_id)
        return

    logger.debug("push_message: chat=%s length=%d", chat_id, length)

    if length >= SHORT_LIMIT:
        try:
            from app.tasks.celery_app import celery
            celery.send_task("summarize_old", args=[chat_id, length])
        except Exception:
            logger.exception("Failed to enqueue summarize_old task")

async def load_context(chat_id: int) -> List[Dict[str, Any]]:
    key_msgs = _key_msgs(chat_id)
    key_sum = _key_summary(chat_id)
    redis = get_redis()

    try:
        async with redis.pipeline() as pipe:
            pipe.get(key_sum)
            pipe.lrange(key_msgs, -SHORT_LIMIT, -1)
            res = await pipe.execute()
            if res is None:
                return []
            summary, raw = res
    except Exception:
        logger.exception("load_context error for chat %s", chat_id)
        return []

    logger.debug("load_context: chat=%s summary_present=%s messages=%d", 
                 chat_id, bool(summary), len(raw or []))

    ctx: List[Dict[str, Any]] = []
    if summary:
        ctx.append({"role": "system", "content": f"Summary: {summary}"})
    for item in raw or []:
        try:
            ctx.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Bad JSON in memory for chat %s: %s", chat_id, item)
    return ctx

async def inc_msg_count(chat_id: int) -> None:
    key = f"msg_count:{chat_id}"
    redis = get_redis()
    try:
        async with redis.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, 86_400)
            if await pipe.execute() is None:
                return
    except Exception:
        logger.exception("inc_msg_count error for chat %s", chat_id)

async def record_activity(chat_id: int, user_id: int) -> None:
    now = time_module.time()
    redis = get_redis()
    try:
        async with redis.pipeline() as pipe:
            pipe.sadd(f"all_users:{chat_id}", user_id)
            pipe.expire(f"all_users:{chat_id}", MEMORY_TTL)
            pipe.zadd(_key_user_last_ts(chat_id), {user_id: now})
            pipe.expire(_key_user_last_ts(chat_id), MEMORY_TTL)
            pipe.set(_key_last_message_ts(chat_id), now)
            pipe.expire(_key_last_message_ts(chat_id), MEMORY_TTL)
            if await pipe.execute() is None:
                return
    except Exception:
        logger.exception("record_activity error for chat %s, user %s", chat_id, user_id)


_GENDER_KEY = lambda user_id: f"user_gender:{user_id}"

async def get_cached_gender(user_id: int) -> str | None:

    raw = await get_redis().get(_GENDER_KEY(user_id))
    return raw if isinstance(raw, str) else (raw.decode() if raw else None)

async def cache_gender(user_id: int, value: str) -> None:

    redis = get_redis()

    if value in ("male", "female"):
        await redis.set(_GENDER_KEY(user_id), value)
    else:
        await redis.set(_GENDER_KEY(user_id), "unknown", ex=86_400)
EOF