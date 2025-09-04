#alembic/env.py
import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from app.config import settings
from app.core.db import Base
import app.core.models

config = context.config
fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline():
    url = os.getenv("DATABASE_URL", settings.DATABASE_URL)
    context.configure(
        url=url,
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
        url=os.getenv("DATABASE_URL", settings.DATABASE_URL),
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