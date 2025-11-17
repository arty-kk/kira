#app/services/user/user_service.py
from __future__ import annotations

import logging

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy import case, and_, or_, func, update

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

async def increment_usage(db: AsyncSession, user_id: int) -> None:
    dec_free = case((User.free_requests > 0, User.free_requests - 1), else_=User.free_requests)
    dec_paid = case(
        (and_(User.free_requests <= 0, User.paid_requests > 0), User.paid_requests - 1),
        else_=User.paid_requests,
    )
    res = await db.execute(
        update(User)
        .where(
            User.id == user_id,
            or_(User.free_requests > 0, User.paid_requests > 0),
        )
        .values(
            free_requests=dec_free,
            paid_requests=dec_paid,
            used_requests=User.used_requests + 1,
        )
        .returning(User.id)
    )
    if res.scalar() is None:
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
    return user.free_requests + user.paid_requests

def get_total_paid_usd(user: User) -> float:
    cents = int(getattr(user, "total_paid_cents", 0) or 0)
    return round(cents / 100.0, 2)