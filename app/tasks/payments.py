from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.bot.utils.telegram_safe import send_message_safe
from app.bot.i18n import t
from app.clients.telegram_client import get_bot
from app.core.db import session_scope
from app.core.memory import push_message
from app.core.models import GiftPurchase, PaymentOutbox, PaymentReceipt, User
from app.services.user.user_service import add_paid_requests, compute_remaining
from app.tasks.celery_app import celery, run_coro_sync
from app.tasks.requeue_result import RequeueResult

logger = logging.getLogger(__name__)
REQUEUE_PENDING_OUTBOX_BATCH_SIZE = 100
OUTBOX_LEASE_TTL_SECONDS = 300
PROCESS_OUTBOX_TIME_LIMIT_SEC = 90
PROCESS_OUTBOX_RUN_TIMEOUT_SEC = 85


def _is_active_lease(outbox: PaymentOutbox) -> bool:
    if outbox.lease_token is None or outbox.leased_at is None:
        return False
    return outbox.leased_at >= datetime.now(timezone.utc) - timedelta(seconds=OUTBOX_LEASE_TTL_SECONDS)


async def _apply_outbox(charge_id: str, lease_token: str) -> tuple[Optional[PaymentOutbox], Optional[int], bool]:
    async with session_scope(stmt_timeout_ms=5000) as db:
        res = await db.execute(
            select(PaymentOutbox)
            .where(PaymentOutbox.telegram_payment_charge_id == charge_id)
            .with_for_update()
        )
        outbox = res.scalar_one_or_none()
        if not outbox:
            logger.warning("payment_outbox: missing charge_id=%s", charge_id)
            return None, None, False

        if outbox.status == "applied":
            if outbox.notified_at is None and outbox.lease_token == lease_token:
                user = await db.get(User, outbox.user_id)
                remaining = compute_remaining(user) if user else None
                return outbox, remaining, True

            logger.info(
                "payment_outbox: stale lease skip charge_id=%s expected_lease_token=%s actual_lease_token=%s",
                charge_id,
                lease_token,
                outbox.lease_token,
            )
            return None, None, False

        if outbox.lease_token != lease_token:
            logger.info(
                "payment_outbox: stale lease skip charge_id=%s expected_lease_token=%s actual_lease_token=%s",
                charge_id,
                lease_token,
                outbox.lease_token,
            )
            return None, None, False

        if not _is_active_lease(outbox):
            logger.info("payment_outbox: no active claim charge_id=%s", charge_id)
            return None, None, False

        outbox.attempts = int(outbox.attempts or 0) + 1
        outbox.last_error = None

        duplicate = False
        user = await db.get(User, outbox.user_id)
        if not user:
            outbox.status = "failed"
            outbox.last_error = "user_not_found"
            return outbox, None, False

        if outbox.requests_amount is None or outbox.requests_amount <= 0:
            outbox.status = "failed"
            outbox.last_error = "invalid_requests_amount"
            return outbox, None, False

        stmt = (
            pg_insert(PaymentReceipt)
            .values(
                user_id=outbox.user_id,
                kind=outbox.kind,
                requests_amount=outbox.requests_amount,
                stars_amount=outbox.stars_amount,
                invoice_payload=outbox.invoice_payload,
                telegram_payment_charge_id=outbox.telegram_payment_charge_id,
                provider_payment_charge_id=outbox.provider_payment_charge_id,
            )
            .on_conflict_do_nothing(index_elements=["telegram_payment_charge_id"])
            .returning(PaymentReceipt.id)
        )
        receipt_id = (await db.execute(stmt)).scalar_one_or_none()
        if receipt_id is None:
            duplicate = True
        else:
            if outbox.kind == "buy":
                await add_paid_requests(db, outbox.user_id, int(outbox.requests_amount or 0))
            else:
                if outbox.gift_code:
                    gp_stmt = (
                        pg_insert(GiftPurchase)
                        .values(
                            user_id=outbox.user_id,
                            gift_code=outbox.gift_code,
                            gift_title=outbox.gift_title,
                            gift_emoji=outbox.gift_emoji,
                            stars_amount=outbox.stars_amount,
                            requests_amount=outbox.requests_amount,
                            invoice_payload=outbox.invoice_payload,
                            telegram_payment_charge_id=outbox.telegram_payment_charge_id,
                        )
                        .on_conflict_do_nothing(index_elements=["telegram_payment_charge_id"])
                    )
                    await db.execute(gp_stmt)
                await add_paid_requests(db, outbox.user_id, int(outbox.requests_amount or 0))

        await db.flush()
        await db.refresh(user)
        remaining = compute_remaining(user)
        outbox.status = "applied"
        outbox.applied_at = datetime.now(timezone.utc)
        logger.info(
            "payment_outbox: applied charge_id=%s user_id=%s kind=%s",
            outbox.telegram_payment_charge_id,
            outbox.user_id,
            outbox.kind,
        )
        return outbox, remaining, duplicate


async def _notify_payment_result(outbox: PaymentOutbox, remaining: Optional[int], duplicate: bool, lease_token: str) -> None:
    if outbox.notified_at is not None:
        return

    async with session_scope(stmt_timeout_ms=3000) as db:
        claim_stmt = (
            update(PaymentOutbox)
            .where(
                PaymentOutbox.id == outbox.id,
                PaymentOutbox.notified_at.is_(None),
                PaymentOutbox.lease_token == lease_token,
            )
            .values(
                leased_at=func.now(),
                updated_at=func.now(),
            )
            .returning(PaymentOutbox.id)
        )
        claimed_id = (await db.execute(claim_stmt)).scalar_one_or_none()

    if claimed_id is None:
        return

    async def _tr(key: str, default: str, **kwargs) -> str:
        try:
            msg = await t(int(outbox.user_id), key, **kwargs)
            return msg or default
        except Exception:
            return default

    if outbox.kind == "buy":
        if duplicate:
            message_text = (
                await _tr("payments.success_duplicate", "", remaining=remaining)
                or await _tr("payments.success", "", req=outbox.requests_amount, remaining=remaining)
                or f"✅ Payment already processed. Remaining: {remaining}"
            )
        else:
            message_text = (
                await _tr("payments.success", "", req=outbox.requests_amount, remaining=remaining)
                or f"✅ Success! +{outbox.requests_amount} requests. Remaining: {remaining}"
            )
    else:
        if duplicate:
            message_text = await _tr("payments.success_duplicate_short", "✅ Payment already processed.")
        else:
            message_text = await _tr("payments.gift_delivered", "✅ Gift delivered.")

    async def _release_notify_lease() -> None:
        async with session_scope(stmt_timeout_ms=3000) as db:
            release_stmt = (
                update(PaymentOutbox)
                .where(
                    PaymentOutbox.id == outbox.id,
                    PaymentOutbox.notified_at.is_(None),
                    PaymentOutbox.lease_token == lease_token,
                )
                .values(lease_token=None, leased_at=None, updated_at=func.now())
            )
            await db.execute(release_stmt)

    try:
        bot = get_bot()
        sent_message = await send_message_safe(bot, int(outbox.user_id), message_text, parse_mode="HTML")
    except Exception:
        await _release_notify_lease()
        raise

    if sent_message is None:
        await _release_notify_lease()

        logger.warning(
            "payment_outbox: уведомление пропущено charge_id=%s user_id=%s",
            outbox.telegram_payment_charge_id,
            outbox.user_id,
        )
        return

    async with session_scope(stmt_timeout_ms=3000) as db:
        finalize_stmt = (
            update(PaymentOutbox)
            .where(
                PaymentOutbox.id == outbox.id,
                PaymentOutbox.notified_at.is_(None),
                PaymentOutbox.lease_token == lease_token,
            )
            .values(notified_at=func.now(), lease_token=None, leased_at=None, updated_at=func.now())
            .returning(PaymentOutbox.id)
        )
        finalized_id = (await db.execute(finalize_stmt)).scalar_one_or_none()

    if finalized_id is None:
        logger.warning(
            "payment_outbox: финализация уведомления пропущена charge_id=%s user_id=%s",
            outbox.telegram_payment_charge_id,
            outbox.user_id,
        )
        return

    outbox.notified_at = datetime.now(timezone.utc)

    if outbox.kind == "gift" and outbox.gift_title and not duplicate:
        gift_label = f"{outbox.gift_emoji or ''} {outbox.gift_title}".strip()
        note = f"[ContextNote] Gift received: {gift_label}."
        try:
            await push_message(
                int(outbox.user_id),
                "system",
                note,
                user_id=int(outbox.user_id),
                namespace="default",
            )
        except Exception:
            logger.exception("payment_outbox: failed to push gift note")

        try:
            celery.send_task(
                "gifts.react",
                args=[
                    int(outbox.user_id),
                    int(outbox.user_id),
                    str(outbox.gift_code or ""),
                    str(gift_label),
                    int(outbox.requests_amount or 0),
                    int(outbox.stars_amount or 0),
                    str(outbox.telegram_payment_charge_id),
                    None,
                    None,
                ],
            )
        except Exception:
            logger.exception("payment_outbox: failed to schedule gift reaction")


@celery.task(
    name="payments.process_outbox",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
    time_limit=PROCESS_OUTBOX_TIME_LIMIT_SEC,
)
def process_outbox_task(charge_id: str, lease_token: str) -> None:
    async def _run_task():
        outbox, remaining, duplicate = await _apply_outbox(charge_id, lease_token)
        if outbox is None:
            return
        if outbox.status != "applied":
            return
        await _notify_payment_result(outbox, remaining, duplicate, lease_token)

    run_coro_sync(_run_task(), timeout=PROCESS_OUTBOX_RUN_TIMEOUT_SEC)


async def requeue_pending_outbox(batch_size: int = REQUEUE_PENDING_OUTBOX_BATCH_SIZE) -> RequeueResult:
    """Requeue pending payment outbox rows for processing.

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
            UPDATE payment_outbox
            SET
                leased_at = now(),
                lease_token = :lease_token,
                lease_attempts = lease_attempts + 1,
                updated_at = now()
            WHERE id IN (
                SELECT id
                FROM payment_outbox
                WHERE status = 'pending'
                  AND applied_at IS NULL
                  AND (leased_at IS NULL OR leased_at < now() - make_interval(secs => :ttl_seconds))
                ORDER BY id
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING telegram_payment_charge_id, lease_token
            """
        )
        claimed_rows = (await db.execute(
            claim_stmt,
            {
                "lease_token": lease_token,
                "ttl_seconds": OUTBOX_LEASE_TTL_SECONDS,
                "batch_size": safe_batch_size,
            },
        )).all()

    enqueued = 0
    enqueue_errors = 0
    for charge_id, row_lease_token in claimed_rows:
        try:
            celery.send_task("payments.process_outbox", args=[str(charge_id), str(row_lease_token)])
            enqueued += 1
        except Exception as exc:
            enqueue_errors += 1
            async with session_scope(stmt_timeout_ms=3000) as db:
                release_stmt = (
                    update(PaymentOutbox)
                    .where(
                        PaymentOutbox.telegram_payment_charge_id == str(charge_id),
                        PaymentOutbox.status == "pending",
                        PaymentOutbox.lease_token == str(row_lease_token),
                    )
                    .values(
                        leased_at=None,
                        lease_token=None,
                        last_error=str(exc),
                    )
                )
                await db.execute(release_stmt)
            logger.exception("payments.requeue_pending_outbox: enqueue failed for charge_id=%s", charge_id)

    logger.info(
        "payments.requeue_pending_outbox: scanned=%s enqueued=%s enqueue_errors=%s batch_size=%s",
        len(claimed_rows),
        enqueued,
        enqueue_errors,
        safe_batch_size,
    )
    return RequeueResult(enqueued=enqueued, enqueue_errors=enqueue_errors)


async def requeue_applied_unnotified_outbox(batch_size: int = REQUEUE_PENDING_OUTBOX_BATCH_SIZE) -> tuple[int, int]:
    safe_batch_size = max(1, int(batch_size))
    lease_token = uuid.uuid4().hex
    async with session_scope(stmt_timeout_ms=5000) as db:
        claim_stmt = text(
            """
            UPDATE payment_outbox
            SET
                leased_at = now(),
                lease_token = :lease_token,
                lease_attempts = lease_attempts + 1,
                updated_at = now()
            WHERE id IN (
                SELECT id
                FROM payment_outbox
                WHERE status = 'applied'
                  AND notified_at IS NULL
                  AND (leased_at IS NULL OR leased_at < now() - make_interval(secs => :ttl_seconds))
                ORDER BY id
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            RETURNING telegram_payment_charge_id, lease_token
            """
        )
        claimed_rows = (await db.execute(
            claim_stmt,
            {
                "lease_token": lease_token,
                "ttl_seconds": OUTBOX_LEASE_TTL_SECONDS,
                "batch_size": safe_batch_size,
            },
        )).all()

    enqueued = 0
    enqueue_errors = 0
    for charge_id, row_lease_token in claimed_rows:
        try:
            celery.send_task("payments.process_outbox", args=[str(charge_id), str(row_lease_token)])
            enqueued += 1
        except Exception as exc:
            enqueue_errors += 1
            async with session_scope(stmt_timeout_ms=3000) as db:
                release_stmt = (
                    update(PaymentOutbox)
                    .where(
                        PaymentOutbox.telegram_payment_charge_id == str(charge_id),
                        PaymentOutbox.status == "applied",
                        PaymentOutbox.lease_token == str(row_lease_token),
                    )
                    .values(
                        leased_at=None,
                        lease_token=None,
                        last_error=str(exc),
                    )
                )
                await db.execute(release_stmt)
            logger.exception("payments.requeue_applied_unnotified_outbox: enqueue failed for charge_id=%s", charge_id)

    logger.info(
        "payments.requeue_applied_unnotified_outbox: scanned=%s enqueued=%s enqueue_errors=%s batch_size=%s",
        len(claimed_rows),
        enqueued,
        enqueue_errors,
        safe_batch_size,
    )
    return enqueued, enqueue_errors


@celery.task(name="payments.requeue_pending_outbox")
def requeue_pending_outbox_task(batch_size: int = REQUEUE_PENDING_OUTBOX_BATCH_SIZE) -> None:
    run_coro_sync(requeue_pending_outbox(batch_size=batch_size))


@celery.task(name="payments.requeue_applied_unnotified_outbox")
def requeue_applied_unnotified_outbox_task(batch_size: int = REQUEUE_PENDING_OUTBOX_BATCH_SIZE) -> None:
    run_coro_sync(requeue_applied_unnotified_outbox(batch_size=batch_size))
