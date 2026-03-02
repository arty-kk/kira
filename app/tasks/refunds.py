from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.models import RefundOutbox
from app.services.user.user_service import InvalidBillingTierError, refund_user_balance
from app.tasks.celery_app import celery, run_coro_sync
from app.tasks.requeue_result import RequeueResult

logger = logging.getLogger(__name__)
REFUND_OUTBOX_LEASE_TTL_SECONDS = 300
REFUND_OUTBOX_MAX_ATTEMPTS = 3
REFUND_REQUEUE_BATCH_SIZE = 100

async def _refund_balance_for_outbox(db: AsyncSession, owner_id: int, billing_tier: str) -> None:
    await refund_user_balance(db, owner_id, billing_tier)


@celery.task(name="refunds.process_outbox")
def process_refund_outbox_task(outbox_id: int, lease_token: str) -> None:
    async def _run_task(lease_token: str) -> None:
        async with session_scope(stmt_timeout_ms=5000) as db:
            res = await db.execute(
                select(RefundOutbox)
                .where(
                    RefundOutbox.id == int(outbox_id),
                    RefundOutbox.status == "pending",
                    RefundOutbox.lease_token == str(lease_token),
                    text("leased_at IS NOT NULL AND leased_at >= now() - make_interval(secs => :lease_ttl_seconds)").bindparams(
                        bindparam("lease_ttl_seconds", REFUND_OUTBOX_LEASE_TTL_SECONDS)
                    ),
                )
                .with_for_update()
            )
            outbox = res.scalar_one_or_none()
            if outbox is None:
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
                if int(outbox.attempts or 0) < REFUND_OUTBOX_MAX_ATTEMPTS:
                    outbox.status = "pending"
                    outbox.leased_at = None
                    outbox.lease_token = None
                else:
                    outbox.status = "failed"
                outbox.last_error = repr(exc)
                outbox.processed_at = None
                logger.exception(
                    "refund_outbox process failed id=%s owner_id=%s request_id=%s",
                    outbox.id,
                    outbox.owner_id,
                    outbox.request_id,
                )

    run_coro_sync(_run_task(str(lease_token)))


async def requeue_pending_refund_outbox(batch_size: int = REFUND_REQUEUE_BATCH_SIZE) -> RequeueResult:
    """Requeue pending refund outbox rows for processing.

    Returns:
        RequeueResult.enqueued: number of rows successfully enqueued to Celery.
        RequeueResult.enqueue_errors: number of claimed rows that failed to enqueue.

    Invariant for the current claimed batch: enqueued + enqueue_errors == scanned.
    """
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
            RETURNING id, lease_token
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
    enqueue_errors = 0
    for outbox_id, row_lease_token in claimed_rows:
        try:
            celery.send_task("refunds.process_outbox", args=[int(outbox_id), str(row_lease_token)])
            enqueued += 1
        except Exception as exc:
            enqueue_errors += 1
            async with session_scope(stmt_timeout_ms=3000) as db:
                release_stmt = (
                    update(RefundOutbox)
                    .where(
                        RefundOutbox.id == int(outbox_id),
                        RefundOutbox.status == "pending",
                        RefundOutbox.lease_token == str(row_lease_token),
                    )
                    .values(
                        leased_at=None,
                        lease_token=None,
                        last_error=str(exc),
                    )
                )
                await db.execute(release_stmt)
            logger.exception("refunds.requeue_pending_refund_outbox: enqueue failed for outbox_id=%s", outbox_id)
            logger.warning(
                "refunds.requeue_pending_refund_outbox: lease released outbox_id=%s lease_token=%s",
                outbox_id,
                row_lease_token,
            )

    logger.info(
        "refunds.requeue_pending_refund_outbox: scanned=%s enqueued=%s enqueue_errors=%s batch_size=%s",
        len(claimed_rows),
        enqueued,
        enqueue_errors,
        safe_batch_size,
    )
    return RequeueResult(enqueued=enqueued, enqueue_errors=enqueue_errors)
