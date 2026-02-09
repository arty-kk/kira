from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.bot.utils.telegram_safe import send_message_safe
from app.bot.i18n import t
from app.clients.telegram_client import get_bot
from app.core.db import session_scope
from app.core.memory import push_message
from app.core.models import GiftPurchase, PaymentOutbox, PaymentReceipt, User
from app.services.user.user_service import add_paid_requests, compute_remaining
from app.tasks.celery_app import celery, _run

logger = logging.getLogger(__name__)
bot = get_bot()


async def _apply_outbox(charge_id: str) -> tuple[Optional[PaymentOutbox], Optional[int], bool]:
    async with session_scope(stmt_timeout_ms=5000) as db:
        res = await db.execute(
            select(PaymentOutbox).where(PaymentOutbox.telegram_payment_charge_id == charge_id).with_for_update()
        )
        outbox = res.scalar_one_or_none()
        if not outbox:
            logger.warning("payment_outbox: missing charge_id=%s", charge_id)
            return None, None, False

        if outbox.status == "applied":
            user = await db.get(User, outbox.user_id)
            remaining = compute_remaining(user) if user else None
            return outbox, remaining, True

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


async def _notify_payment_result(outbox: PaymentOutbox, remaining: Optional[int], duplicate: bool) -> None:
    if outbox.notified_at is not None:
        return

    async def _tr(key: str, default: str, **kwargs) -> str:
        try:
            msg = await t(int(outbox.user_id), key, **kwargs)
            return msg or default
        except Exception:
            return default

    if outbox.kind == "buy":
        if duplicate:
            text = (
                await _tr("payments.success_duplicate", "", remaining=remaining)
                or await _tr("payments.success", "", req=outbox.requests_amount, remaining=remaining)
                or f"✅ Payment already processed. Remaining: {remaining}"
            )
        else:
            text = (
                await _tr("payments.success", "", req=outbox.requests_amount, remaining=remaining)
                or f"✅ Success! +{outbox.requests_amount} requests. Remaining: {remaining}"
            )
    else:
        if duplicate:
            text = await _tr("payments.success_duplicate_short", "✅ Payment already processed.")
        else:
            text = await _tr("payments.gift_delivered", "✅ Gift delivered.")

    await send_message_safe(bot, int(outbox.user_id), text, parse_mode="HTML")

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

    async with session_scope(stmt_timeout_ms=3000) as db:
        res = await db.execute(
            select(PaymentOutbox).where(PaymentOutbox.id == outbox.id).with_for_update()
        )
        row = res.scalar_one_or_none()
        if row and row.notified_at is None:
            row.notified_at = datetime.now(timezone.utc)


@celery.task(name="payments.process_outbox", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 5})
def process_outbox_task(charge_id: str) -> None:
    async def _run_task():
        outbox, remaining, duplicate = await _apply_outbox(charge_id)
        if outbox is None:
            return
        if outbox.status != "applied":
            return
        await _notify_payment_result(outbox, remaining, duplicate)

    _run(_run_task())
