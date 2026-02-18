#app/core/db.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import event
from sqlalchemy.pool import NullPool
from app.config import settings
from sqlalchemy import text
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

pool_kwargs = {"pool_pre_ping": True}
use_nullpool = (settings.DB_POOL_CLASS or "").lower() == "nullpool"
if use_nullpool:
    pool_kwargs["poolclass"] = NullPool
else:
    if getattr(settings, "DB_POOL_USE_LIFO", True):
        pool_kwargs["pool_use_lifo"] = True
    pool_kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
    )

connect_args = {}
app_name = getattr(settings, "DB_APP_NAME", "") or None
if app_name:
    connect_args = {"server_settings": {"application_name": app_name}}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    **pool_kwargs,
)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()

@asynccontextmanager
async def get_db() -> AsyncIterator[AsyncSession]:
    session = AsyncSessionLocal()
    try:
        try:
            yield session
            if session.in_transaction():
                await session.commit()
        except Exception:
            if session.in_transaction():
                await session.rollback()
            raise
        finally:
            if session.in_transaction():
                with suppress(Exception):
                    await session.rollback()
    finally:
        await session.close()

@asynccontextmanager
async def session_scope(*, stmt_timeout_ms: int | None = None, read_only: bool = False, autocommit: bool = True):
    session = AsyncSessionLocal()
    try:
        try:
            if stmt_timeout_ms and stmt_timeout_ms > 0:
                await session.execute(text(f"SET LOCAL statement_timeout = {int(stmt_timeout_ms)}"))
                await session.execute(text("SET LOCAL lock_timeout = 1000"))
            if read_only:
                await session.execute(text("SET LOCAL default_transaction_read_only = on"))

            yield session

            if session.in_transaction():
                if read_only:
                    await session.commit()
                elif autocommit:
                    await session.commit()

        except Exception:
            with suppress(Exception):
                if session.in_transaction():
                    await session.rollback()
            raise
        finally:
            if session.in_transaction():
                with suppress(Exception):
                    await session.rollback()
    finally:
        await session.close()

try:
    slow_ms = int(getattr(settings, "DB_LOG_SLOW_MS", 0) or 0)
except Exception:
    slow_ms = 0

if slow_ms > 0:
    import time
    import logging
    log = logging.getLogger(__name__)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before_execute(conn, cursor, statement, parameters, context, executemany):
        context._q_start = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after_execute(conn, cursor, statement, parameters, context, executemany):
        try:
            dur = (time.perf_counter() - getattr(context, "_q_start", time.perf_counter())) * 1000
            if dur >= slow_ms:
                log.warning("SLOW SQL (%.1f ms): %s", dur, statement[:500])
        except Exception:
            pass
