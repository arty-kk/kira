from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.models import RefundOutbox, User
from app.tasks.celery_app import celery, _run

logger = logging.getLogger(__name__)
REFUND_OUTBOX_LEASE_TTL_SECONDS = 300
REFUND_REQUEUE_BATCH_SIZE = 100


class InvalidBillingTierError(ValueError):
    pass


async def _refund_balance_for_outbox(db: AsyncSession, owner_id: int, billing_tier: str) -> None:
    if billing_tier not in ("free", "paid"):
        raise InvalidBillingTierError("invalid_billing_tier")
    res = await db.execute(select(User).where(User.id == owner_id).with_for_update())
    user = res.scalar_one_or_none()
    if not user:
        return
    if billing_tier == "free":
        user.free_requests += 1
    elif billing_tier == "paid":
        user.paid_requests += 1
    if user.used_requests > 0:
        user.used_requests -= 1
    await db.flush()


@celery.task(name="refunds.process_outbox")
def process_refund_outbox_task(outbox_id: int) -> None:
    async def _run_task() -> None:
        async with session_scope(stmt_timeout_ms=5000) as db:
            res = await db.execute(
                select(RefundOutbox)
                .where(RefundOutbox.id == int(outbox_id))
                .with_for_update()
            )
            outbox = res.scalar_one_or_none()
            if outbox is None or outbox.status != "pending":
                return

            outbox.attempts = int(outbox.attempts or 0) + 1
            outbox.leased_at = None
            outbox.lease_token = None
            try:
                async with db.begin_nested():
                    await _refund_balance_for_outbox(db, int(outbox.owner_id), str(outbox.billing_tier or ""))
                    outbox.status = "applied"
                    outbox.last_error = None
                    outbox.processed_at = datetime.now(timezone.utc)
            except InvalidBillingTierError:
                outbox.status = "failed"
                outbox.last_error = "invalid_billing_tier"
                outbox.processed_at = None
                logger.warning(
                    "refund_outbox validation failed id=%s owner_id=%s request_id=%s code=invalid_billing_tier",
                    outbox.id,
                    outbox.owner_id,
                    outbox.request_id,
                )
            except Exception as exc:
                outbox.status = "failed"
                outbox.last_error = repr(exc)
                logger.exception(
                    "refund_outbox process failed id=%s owner_id=%s request_id=%s",
                    outbox.id,
                    outbox.owner_id,
                    outbox.request_id,
                )

    _run(_run_task())


async def requeue_pending_refund_outbox(batch_size: int = REFUND_REQUEUE_BATCH_SIZE) -> tuple[int, int]:
    safe_batch_size = max(1, int(batch_size))
    lease_token = uuid.uuid4().hex
    async with session_scope(stmt_timeout_ms=5000) as db:
        claim_stmt = text(
            """
            UPDATE refund_outbox
            SET
                leased_at = now(),
                lease_token = :lease_token,
                lease_attempts = lease_attempts + 1,
                updated_at = now()
            WHERE id IN (
                SELECT id
                FROM refund_outbox
                WHERE status = 'pending'
                  AND (leased_at IS NULL OR leased_at < now() - make_interval(secs => :ttl_seconds))
                ORDER BY id
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """
        )
        claimed_rows = (await db.execute(
            claim_stmt,
            {
                "lease_token": lease_token,
                "ttl_seconds": REFUND_OUTBOX_LEASE_TTL_SECONDS,
                "batch_size": safe_batch_size,
            },
        )).all()

    enqueued = 0
    for (outbox_id,) in claimed_rows:
        try:
            celery.send_task("refunds.process_outbox", args=[int(outbox_id)])
            enqueued += 1
        except Exception:
            logger.exception("refunds.requeue_pending_refund_outbox: enqueue failed for outbox_id=%s", outbox_id)

    logger.info(
        "refunds.requeue_pending_refund_outbox: scanned=%s enqueued=%s batch_size=%s",
        len(claimed_rows),
        enqueued,
        safe_batch_size,
    )
    return len(claimed_rows), enqueued
