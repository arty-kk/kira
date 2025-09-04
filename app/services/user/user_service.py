#app/services/user/user_service.py
from __future__ import annotations

import logging

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from aiogram.types import User as TelegramUser

from app.core.models import User

logger = logging.getLogger(__name__)

async def get_or_create_user(db: AsyncSession, tg_user: TelegramUser) -> User:

    result = await db.execute(select(User).where(User.id == tg_user.id))
    user = result.scalars().first()

    if user:
        return user

    user = User(
        id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name,
    )
    db.add(user)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(select(User).where(User.id == tg_user.id))
        user = result.scalars().first()
        if not user:
            raise
    else:
        await db.refresh(user)
        logger.info("Created new user %s (%s)", user.id, user.username)

    return user

async def increment_usage(db: AsyncSession, user_id: int) -> None:

    try:
        user = await db.get(User, user_id)
        if user is None:
            logger.warning("User %s not found", user_id)
            return

        if user.free_requests_left > 0:
            user.free_requests_left -= 1
        elif user.paid_requests > 0:
            user.paid_requests -= 1
        else:
            logger.warning("User %s tried to use request with zero balance", user_id)
            return

        user.used_requests += 1
        await db.commit()
    except Exception:
        logger.exception("increment_usage failed for user %s", user_id)
        await db.rollback()

async def add_paid_requests(db: AsyncSession, user_id: int, amount: int) -> None:
    
    try:
        user = await db.get(User, user_id)
        if user is None:
            logger.warning("User %s not found", user_id)
            return

        inc_requests = max(int(amount), 0)
        if inc_requests <= 0:
            await db.commit()
            return

        user.paid_requests += inc_requests
        user.total_paid_cents = (user.total_paid_cents or 0) + inc_requests * 12
        await db.commit()
    except Exception:
        logger.exception("add_paid_requests failed for user %s", user_id)
        await db.rollback()

def compute_remaining(user: User) -> int:
    return user.free_requests_left + user.paid_requests

def get_total_paid_usd(user: User) -> float:
    cents = int(getattr(user, "total_paid_cents", 0) or 0)
    return round(cents / 100.0, 2)
