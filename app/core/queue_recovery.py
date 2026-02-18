from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass


_REQUEUE_ON_START_LUA = """
local processing_key = KEYS[1]
local queue_key = KEYS[2]

local pending = redis.call('LRANGE', processing_key, 0, -1)
local moved = #pending

if moved > 0 then
  redis.call('RPUSH', queue_key, unpack(pending))
  redis.call('LTRIM', processing_key, moved, -1)
end

return moved
"""


@dataclass(frozen=True)
class QueueRecoveryResult:
    moved_count: int
    lock_acquired: bool


def _is_watch_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "WatchError"


def _is_eval_unavailable(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        isinstance(exc, (AttributeError, NotImplementedError))
        or "unknown command" in message
        or " eval " in f" {message} "
        or "noscript" in message
    )


async def requeue_processing_on_start(
    redis,
    *,
    queue_key: str,
    processing_key: str,
    lock_ttl: int,
) -> QueueRecoveryResult:
    requeue_lock_key = f"{processing_key}:requeue_lock"

    lock_acquired = bool(
        await redis.set(
            requeue_lock_key,
            1,
            nx=True,
            ex=lock_ttl,
        )
    )
    if not lock_acquired:
        return QueueRecoveryResult(moved_count=0, lock_acquired=False)

    try:
        moved = await redis.eval(
            _REQUEUE_ON_START_LUA,
            2,
            processing_key,
            queue_key,
        )
        return QueueRecoveryResult(moved_count=int(moved or 0), lock_acquired=True)
    except Exception as exc:
        if not _is_eval_unavailable(exc):
            raise

    for _ in range(3):
        pipe = redis.pipeline()
        try:
            await pipe.watch(processing_key)
            pending = await pipe.lrange(processing_key, 0, -1)
            moved = len(pending)
            if moved <= 0:
                return QueueRecoveryResult(moved_count=0, lock_acquired=True)

            pipe.multi()
            pipe.rpush(queue_key, *pending)
            pipe.ltrim(processing_key, moved, -1)
            await pipe.execute()
            return QueueRecoveryResult(moved_count=moved, lock_acquired=True)
        except Exception as exc:
            if _is_watch_error(exc):
                continue
            raise
        finally:
            with suppress(Exception):
                await pipe.reset()

    return QueueRecoveryResult(moved_count=0, lock_acquired=True)
