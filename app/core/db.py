#app/core/db.py
from __future__ import annotations

import logging
import time
import hashlib
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from pgvector.psycopg import register_vector, register_vector_async
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

logger = logging.getLogger(__name__)


# --- Engine / pool ---------------------------------------------------------

def _normalize_database_url(raw_url: str) -> str:
    """Normalize DSN to SQLAlchemy async psycopg URL."""
    url = str(raw_url or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is empty")

    # Standardize driver for PostgreSQL + SQLAlchemy asyncio.
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]

    return url


def _build_connect_args() -> dict:
    connect_args: dict[str, object] = {}

    options: list[str] = []
    app_name = str(getattr(settings, "DB_APP_NAME", "") or "").strip()
    if app_name:
        options.append(f"-c application_name={app_name}")

    if options:
        connect_args["options"] = " ".join(options)

    # Conservative defaults for long-running workers.
    connect_args.setdefault("connect_timeout", 5)
    return connect_args


def _build_pool_kwargs() -> dict:
    pool_kwargs: dict[str, object] = {"pool_pre_ping": True}

    use_nullpool = (str(getattr(settings, "DB_POOL_CLASS", "QueuePool")) or "").lower() == "nullpool"
    if use_nullpool:
        pool_kwargs["poolclass"] = NullPool
        return pool_kwargs

    if bool(getattr(settings, "DB_POOL_USE_LIFO", True)):
        pool_kwargs["pool_use_lifo"] = True

    pool_kwargs.update(
        pool_size=int(getattr(settings, "DB_POOL_SIZE", 200) or 200),
        max_overflow=int(getattr(settings, "DB_MAX_OVERFLOW", 0) or 0),
        pool_timeout=int(getattr(settings, "DB_POOL_TIMEOUT", 2) or 2),
        pool_recycle=int(getattr(settings, "DB_POOL_RECYCLE", 1800) or 1800),
    )
    return pool_kwargs


DATABASE_URL = _normalize_database_url(settings.DATABASE_URL)
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    hide_parameters=True,
    connect_args=_build_connect_args(),
    **_build_pool_kwargs(),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# --- Driver registration / DB bootstrap -----------------------------------

def _register_pgvector_adapters(dbapi_connection) -> None:
    run_async = getattr(dbapi_connection, "run_async", None)
    if callable(run_async):
        run_async(register_vector_async)
        return
    register_vector(dbapi_connection)


@event.listens_for(engine.sync_engine, "connect")
def _on_connect_register_pgvector(dbapi_connection, connection_record):
    # Skip non-psycopg connections (e.g. sqlite in tests).
    dbapi_mod = getattr(type(dbapi_connection), "__module__", "")
    if "psycopg" not in dbapi_mod:
        return

    info = getattr(connection_record, "info", None)
    if isinstance(info, dict) and info.get("pgvector_registered"):
        return

    try:
        _register_pgvector_adapters(dbapi_connection)
        if isinstance(info, dict):
            info["pgvector_registered"] = True
    except Exception:
        logger.exception("failed to register pgvector adapters on db connection")
        raise


# Backward-compatible alias for existing internal references/tests.
def _on_connect(dbapi_connection, connection_record):
    _on_connect_register_pgvector(dbapi_connection, connection_record)


async def initialize_postgres(*, ensure_extensions: bool = True, target_engine=None) -> None:
    """Warm up DB connection and verify PostgreSQL capabilities.

    Designed for PostgreSQL 16 + pgvector runtime.
    """
    eng = target_engine or engine
    async with eng.begin() as conn:
        dialect = conn.dialect.name
        if dialect != "postgresql":
            logger.info("DB init skipped: non-PostgreSQL dialect=%s", dialect)
            return

        ver_num = (await conn.execute(text("SHOW server_version_num"))).scalar_one()
        try:
            ver_num_i = int(ver_num)
        except Exception:
            ver_num_i = 0

        if ver_num_i < 160000:
            logger.warning("PostgreSQL <16 detected (server_version_num=%s); expected 16+", ver_num)

        if ensure_extensions:
            # vector is required for RAG. pg_trgm/unaccent are useful optional companions.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))

        logger.info("DB init completed: dialect=%s server_version_num=%s", dialect, ver_num)


# --- Session helpers -------------------------------------------------------
@asynccontextmanager
async def session_scope(
    *,
    stmt_timeout_ms: int | None = None,
    read_only: bool = False,
    autocommit: bool = True,
) -> AsyncIterator[AsyncSession]:
    session = AsyncSessionLocal()
    try:
        try:
            first_init_started_at: float | None = None
            first_init_label: str | None = None

            async def _execute_init(statement, label: str):
                nonlocal first_init_started_at, first_init_label
                is_first_init = first_init_started_at is None
                if is_first_init:
                    first_init_started_at = time.perf_counter()
                    first_init_label = label
                await session.execute(statement)
                if is_first_init and first_init_started_at is not None:
                    duration_ms = (time.perf_counter() - first_init_started_at) * 1000
                    log_extra = {
                        "phase": "session_scope_init",
                        "init_step": first_init_label,
                        "duration_ms": round(duration_ms, 2),
                        "read_only": bool(read_only),
                        "stmt_timeout_ms": int(stmt_timeout_ms or 0),
                    }
                    if duration_ms >= 200:
                        logger.info("session_scope init first command completed", extra=log_extra)
                    else:
                        logger.debug("session_scope init first command completed", extra=log_extra)

            if stmt_timeout_ms and stmt_timeout_ms > 0:
                await _execute_init(text(f"SET LOCAL statement_timeout = {int(stmt_timeout_ms)}"), "set_statement_timeout")
                await session.execute(text("SET LOCAL lock_timeout = 1000"))
            if read_only:
                await _execute_init(text("SET LOCAL default_transaction_read_only = on"), "set_read_only")

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


# --- Slow SQL logger -------------------------------------------------------
try:
    slow_ms = int(getattr(settings, "DB_LOG_SLOW_MS", 0) or 0)
except Exception:
    slow_ms = 0

if slow_ms > 0:

    def _sql_fingerprint(statement: str) -> tuple[str, str]:
        """Return safe SQL metadata without leaking raw query text/values."""
        raw = str(statement or "")
        compact = " ".join(raw.split())
        op = (compact.split(" ", 1)[0].upper() if compact else "UNKNOWN")[:24]
        digest = hashlib.sha256(compact.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return op, digest

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before_execute(conn, cursor, statement, parameters, context, executemany):
        context._q_start = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after_execute(conn, cursor, statement, parameters, context, executemany):
        try:
            dur = (time.perf_counter() - getattr(context, "_q_start", time.perf_counter())) * 1000
            if dur >= slow_ms:
                op, fingerprint = _sql_fingerprint(statement)
                logger.warning(
                    "SLOW SQL (%.1f ms): op=%s fingerprint=%s",
                    dur,
                    op,
                    fingerprint,
                )
        except Exception:
            pass
