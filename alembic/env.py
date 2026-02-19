#alembic/env.py
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config


config = context.config
fileConfig(config.config_file_name)


def _get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run Alembic migrations")
    return database_url


def _get_target_metadata():
    from app.core import models as _models  # noqa: F401

    return _models.Base.metadata


target_metadata = _get_target_metadata()


def run_migrations_offline():
    context.configure(
        url=_get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        url=_get_database_url(),
        future=True,
    )

    def do_sync_migrations(sync_connection):
        context.configure(
            connection=sync_connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    async def run():
        async with connectable.connect() as async_connection:
            await async_connection.run_sync(do_sync_migrations)

    asyncio.run(run())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
