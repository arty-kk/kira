#alembic/env.py
import asyncio

from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from app.config import settings
from app.core import Base

import app.core.models

config = context.config
fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline():
    url = settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        url=settings.DATABASE_URL,
        future=True,
    )

    async def do_run_migrations(connection):
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        async with connection.begin():
            await context.run_migrations()

    async def run():
        async with connectable.connect() as connection:
            await do_run_migrations(connection)

    asyncio.run(run())