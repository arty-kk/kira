#app/services/user/user_service.py
from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func, select, update

from app.core.models import RequestReservation, User

if TYPE_CHECKING:
    from aiogram.types import User as TelegramUser
from app.core.db import session_scope

logger = logging.getLogger(__name__)

PRICE_PER_REQUEST_MILLS = 56


class InvalidBillingTierError(ValueError):
    pass

async def get_or_create_user(db: AsyncSession, tg_user: "TelegramUser") -> User:
    stmt = (
        insert(User)
        .values(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
        )
        .on_conflict_do_update(
            index_elements=[User.id],
            set_={
                "username": tg_user.username,
                "full_name": tg_user.full_name,
            },
        )
        .returning(User.id)
    )
    user_id = (await db.execute(stmt)).scalar_one()

    user = await db.get(User, user_id)
    return user

@dataclass(frozen=True)
class ConsumeResult:
    consumed: bool
    used_paid: bool


@dataclass(frozen=True)
class ReserveResult:
    reserved: bool
    used_paid: bool
    reservation_id: int | None

async def consume_request(db: AsyncSession, user_id: int, *, prefer_paid: bool = False) -> ConsumeResult:

    if prefer_paid:
        res = await db.execute(
            update(User)
            .where(User.id == user_id, User.paid_requests > 0)
            .values(
                paid_requests=User.paid_requests - 1,
                used_requests=User.used_requests + 1,
            )
            .returning(User.id)
        )
        if res.scalar() is not None:
            return ConsumeResult(consumed=True, used_paid=True)

        res2 = await db.execute(
            update(User)
            .where(User.id == user_id, User.free_requests > 0)
            .values(
                free_requests=User.free_requests - 1,
                used_requests=User.used_requests + 1,
            )
            .returning(User.id)
        )
        return ConsumeResult(consumed=(res2.scalar() is not None), used_paid=False)

    res = await db.execute(
        update(User)
        .where(User.id == user_id, User.free_requests > 0)
        .values(
            free_requests=User.free_requests - 1,
            used_requests=User.used_requests + 1,
        )
        .returning(User.id)
    )
    if res.scalar() is not None:
        return ConsumeResult(consumed=True, used_paid=False)

    res2 = await db.execute(
        update(User)
        .where(User.id == user_id, User.paid_requests > 0)
        .values(
            paid_requests=User.paid_requests - 1,
            used_requests=User.used_requests + 1,
        )
        .returning(User.id)
    )
    if res2.scalar() is not None:
        return ConsumeResult(consumed=True, used_paid=True)
    return ConsumeResult(consumed=False, used_paid=False)


async def reserve_request(
    db: AsyncSession,
    user_id: int,
    *,
    prefer_paid: bool = False,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> ReserveResult:
    res = await consume_request(db, user_id, prefer_paid=prefer_paid)
    if not res.consumed:
        return ReserveResult(reserved=False, used_paid=False, reservation_id=None)

    reservation = RequestReservation(
        user_id=user_id,
        status="reserved",
        used_paid=bool(res.used_paid),
        chat_id=chat_id,
        message_id=message_id,
    )
    db.add(reservation)
    await db.flush()
    return ReserveResult(
        reserved=True,
        used_paid=bool(res.used_paid),
        reservation_id=reservation.id,
    )


async def confirm_reservation(db: AsyncSession, reservation_id: int) -> None:
    if not reservation_id:
        return
    res = await db.execute(
        update(RequestReservation)
        .where(RequestReservation.id == reservation_id)
        .where(RequestReservation.status == "reserved")
        .values(status="consumed")
        .returning(RequestReservation.id)
    )
    if res.scalar_one_or_none() is None:
        return


async def refund_reservation(db: AsyncSession, reservation_id: int) -> None:
    if not reservation_id:
        return
    res = await db.execute(
        update(RequestReservation)
        .where(RequestReservation.id == reservation_id)
        .where(RequestReservation.status == "reserved")
        .values(status="refunded")
        .returning(
            RequestReservation.user_id,
            RequestReservation.used_paid,
        )
    )
    row = res.one_or_none()
    if row is None:
        return

    user_id, used_paid = row
    if used_paid:
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                paid_requests=User.paid_requests + 1,
                used_requests=func.greatest(User.used_requests - 1, 0),
            )
        )
    else:
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                free_requests=User.free_requests + 1,
                used_requests=func.greatest(User.used_requests - 1, 0),
            )
        )


async def confirm_reservation_by_id(reservation_id: int) -> None:
    if not reservation_id:
        return
    async with session_scope(stmt_timeout_ms=2000) as db:
        await confirm_reservation(db, reservation_id)


async def refund_reservation_by_id(reservation_id: int) -> None:
    if not reservation_id:
        return
    async with session_scope(stmt_timeout_ms=2000) as db:
        await refund_reservation(db, reservation_id)


async def refund_user_balance(db: AsyncSession, owner_id: int, billing_tier: str | None) -> None:
    """Refund one consumed request for a user.

    Invariants: only "free"/"paid" billing tiers are valid; missing user is a no-op;
    used_requests never goes below zero.
    """
    if billing_tier not in ("free", "paid"):
        raise InvalidBillingTierError("invalid_billing_tier")

    res = await db.execute(select(User).where(User.id == owner_id).with_for_update())
    user = res.scalar_one_or_none()
    if not user:
        return

    if billing_tier == "free":
        user.free_requests += 1
    else:
        user.paid_requests += 1
    if user.used_requests > 0:
        user.used_requests -= 1
    await db.flush()

async def increment_usage(db: AsyncSession, user_id: int) -> None:
    r = await consume_request(db, user_id, prefer_paid=False)
    if not r.consumed:
        logger.warning("User %s tried to use request with zero balance", user_id)

async def add_paid_requests(db: AsyncSession, user_id: int, amount: int) -> None:
    inc_requests = max(int(amount), 0)
    if inc_requests <= 0:
        return
    inc_mills = inc_requests * PRICE_PER_REQUEST_MILLS
    inc_cents = (inc_mills + 5) // 10
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            paid_requests=User.paid_requests + inc_requests,
            total_paid_cents=(func.coalesce(User.total_paid_cents, 0) + int(inc_cents)),
        )
    )

def compute_remaining(user: User) -> int:
    free = int(getattr(user, "free_requests", 0) or 0)
    paid = int(getattr(user, "paid_requests", 0) or 0)
    return max(0, free + paid)

def get_total_paid_usd(user: User) -> float:
    cents = int(getattr(user, "total_paid_cents", 0) or 0)
    return round(cents / 100.0, 2)
