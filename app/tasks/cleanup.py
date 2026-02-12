#app/tasks/cleanup.py
from __future__ import annotations

import logging
import contextlib
import asyncio
import uuid

from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from app.core.models import User
from app.core.db import session_scope
from app.core.memory import get_redis, delete_user_redis_data, is_recently_active
from app.config import settings

logger = logging.getLogger(__name__)

_RENEW_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
else
  return 0
end
"""

async def _iter_nonbuyer_ids(cutoff, page_size: int = 5000):
    last_id = 0
    while True:
        async with session_scope(stmt_timeout_ms=10000, read_only=True) as db:
            res = await db.execute(
                select(User.id)
                .where(
                    User.total_paid_cents == 0,
                    User.registered_at < cutoff,
                    User.id > last_id,
                )
                .order_by(User.id)
                .limit(page_size)
            )
            ids = [int(x) for x in res.scalars().all()]
        if not ids:
            break
        yield ids
        last_id = ids[-1]

async def cleanup_nonbuyers() -> None:
    r = get_redis()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    try:
        active_window_days = int(getattr(settings, "CLEANUP_ACTIVE_WINDOW_DAYS", 14))
    except Exception:
        active_window_days = 14

    lock_key = "cleanup_nonbuyers:lock"
    owner_token = str(uuid.uuid4())
    lock_ttl = int(getattr(settings, "CLEANUP_LOCK_TTL_SEC", 1800))
    if not await r.set(lock_key, owner_token, ex=lock_ttl, nx=True):
        return

    async def _renew_lock():
        interval = max(30, int(getattr(settings, "CLEANUP_LOCK_RENEW_INTERVAL_SEC", lock_ttl // 4 or 60)))
        while True:
            try:
                await asyncio.sleep(interval)
                ok = await r.eval(_RENEW_LOCK_LUA, 1, lock_key, owner_token, int(lock_ttl * 1000))
                if int(ok or 0) == 0:
                    break
            except Exception:
                break
    renew_task = asyncio.create_task(_renew_lock())

    processed = 0
    total_deleted_keys = 0
    total_swept_keys = 0

    try:
        delete_conc = int(getattr(settings, "CLEANUP_DELETE_CONCURRENCY", 8))
    except Exception:
        delete_conc = 8
    sem = asyncio.Semaphore(max(1, delete_conc))

    async def _deep_delete(uid: int) -> int:
        try:
            async with sem:
                try:
                    if await r.exists(f"personal_enrolled:{uid}") or await is_recently_active(uid, within_days=active_window_days):
                        return 0
                except Exception:
                    return 0
                try:
                    return await delete_user_redis_data(uid)
                except Exception:
                    logger.exception("cleanup_nonbuyers: failed to delete redis data for user_id=%s", uid)
                    return 0
        except Exception:
            return 0

    try:
        async for user_ids in _iter_nonbuyer_ids(cutoff, page_size=5000):
            processed += len(user_ids)

            BATCH = 1000
            for i in range(0, len(user_ids), BATCH):
                chunk = user_ids[i : i + BATCH]

                try:
                    keys = [f"personal_enrolled:{uid}" for uid in chunk]
                    async with r.pipeline(transaction=False) as p:
                        for k in keys:
                            p.exists(k)
                        exists_flags = await p.execute()
                except Exception:
                    try:
                        await asyncio.sleep(0.25)
                        async with r.pipeline(transaction=False) as p:
                            for k in [f"personal_enrolled:{uid}" for uid in chunk]:
                                p.exists(k)
                            exists_flags = await p.execute()
                    except Exception:
                        logger.warning("cleanup_nonbuyers: EXISTS failed twice for chunk; skipping this chunk")
                        continue

                sweep = []
                for uid, enrolled in zip(chunk, exists_flags):
                    if int(enrolled) == 1:
                        continue
                    try:
                        if not await is_recently_active(uid, within_days=active_window_days):
                            sweep.append(uid)
                    except Exception as exc:
                        logger.warning(
                            "cleanup_nonbuyers: ошибка проверки активности в cleanup_nonbuyers; user_id=%s; error=%r",
                            uid,
                            exc,
                        )

                if not sweep:
                    continue
                async with r.pipeline(transaction=False) as p:
                    for uid in sweep:
                        p.zrem("personal_ping_schedule", str(uid))
                    resp = await p.execute()
                try:
                    total_swept_keys += sum(int(x or 0) for x in resp if isinstance(x, int))
                except Exception:
                    pass
                tasks = [_deep_delete(uid) for uid in sweep]
                if tasks:
                    try:
                        for res in await asyncio.gather(*tasks, return_exceptions=True):
                            if isinstance(res, int):
                                total_deleted_keys += res
                    except Exception:
                        logger.exception("cleanup_nonbuyers: deep delete gather failed")

        logger.info(
            "cleanup_nonbuyers: processed_users=%d, swept_keys=%d, deep_deleted_keys=%d (safe-skip active)",
            processed, total_swept_keys, total_deleted_keys,
         )
    finally:
        with contextlib.suppress(Exception):
            try:
                renew_task.cancel()
            except Exception:
                pass
        with contextlib.suppress(Exception):
            val = await r.get(lock_key)
            try:
                cur = val.decode() if isinstance(val, (bytes, bytearray)) else val
            except Exception:
                cur = val
            if cur == owner_token:
                await r.delete(lock_key)
