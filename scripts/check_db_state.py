#!/usr/bin/env python3
"""Smoke-check PostgreSQL schema state after migrations."""

import asyncio
import os
import sys

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

EXPECTED_ALEMBIC_REVISION = "0001_initial_schema"
REQUIRED_TABLES = ("users", "refund_outbox", "rag_tag_vectors")


def fail(category: str, message: str) -> None:
    print(f"{category}: {message}", file=sys.stderr)
    raise SystemExit(1)


async def _run_check(database_url: str) -> None:
    current_schema = ""
    engine = create_async_engine(database_url, echo=False)
    try:
        async with engine.connect() as conn:
            current_schema = (
                await conn.execute(sa.text("select current_schema()"))
            ).scalar_one_or_none()
            if not current_schema:
                fail("schema mismatch", "current_schema() returned empty value")

            for table in REQUIRED_TABLES:
                present = (
                    await conn.execute(
                        sa.text("select to_regclass(current_schema() || '.' || :table) is not null"),
                        {"table": table},
                    )
                ).scalar_one()
                if not present:
                    fail(
                        "missing table",
                        f"{current_schema}.{table} is not available in current schema",
                    )

            vector_extension = (
                await conn.execute(
                    sa.text("select exists(select 1 from pg_extension where extname = 'vector')")
                )
            ).scalar_one()
            if not vector_extension:
                fail("missing extension", "pg_extension does not contain 'vector'")

            alembic_in_current = (
                await conn.execute(
                    sa.text("select to_regclass(current_schema() || '.alembic_version') is not null")
                )
            ).scalar_one()
            if not alembic_in_current:
                alembic_in_public = (
                    await conn.execute(sa.text("select to_regclass('public.alembic_version') is not null"))
                ).scalar_one()
                if alembic_in_public:
                    fail(
                        "schema mismatch",
                        "alembic_version is present in public schema but not in current schema",
                    )
                fail("schema mismatch", "alembic_version is missing in current schema")

            quoted_schema = conn.dialect.identifier_preparer.quote_identifier(current_schema)
            current_schema_revision = (
                await conn.execute(sa.text(f"select version_num from {quoted_schema}.alembic_version"))
            ).scalar_one_or_none()
            if current_schema_revision is None:
                public_revision = (
                    await conn.execute(sa.text("select version_num from public.alembic_version"))
                ).scalar_one_or_none()
                if public_revision is not None:
                    fail(
                        "schema mismatch",
                        "alembic_version is present in public schema but not in current schema",
                    )
                fail("schema mismatch", "alembic_version is missing in current schema")

            if current_schema_revision != EXPECTED_ALEMBIC_REVISION:
                fail(
                    "unexpected alembic revision",
                    f"expected {EXPECTED_ALEMBIC_REVISION}, got {current_schema_revision}",
                )

    except SQLAlchemyError as exc:
        fail("schema mismatch", f"database check failed: {exc}")
    finally:
        await engine.dispose()

    print(
        "db smoke-check passed: "
        f"schema={current_schema}, "
        f"revision={EXPECTED_ALEMBIC_REVISION}, "
        "required_tables=users,refund_outbox,rag_tag_vectors, "
        "extension=vector"
    )


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        fail("schema mismatch", "DATABASE_URL is not set")
    asyncio.run(_run_check(database_url))


if __name__ == "__main__":
    main()
