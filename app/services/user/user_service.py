#app/services/user/user_service.py
from __future__ import annotations

import logging

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func, update

from aiogram.types import User as TelegramUser

from app.core.models import User

logger = logging.getLogger(__name__)

PRICE_PER_REQUEST_MILLS = 56

async def get_or_create_user(db: AsyncSession, tg_user: TelegramUser) -> User:
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