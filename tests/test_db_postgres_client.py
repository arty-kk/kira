import unittest
from unittest.mock import AsyncMock

from app.core import db as db_mod


class DbPostgresClientTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_database_url_for_psycopg(self):
        self.assertEqual(
            db_mod._normalize_database_url("postgresql://u:p@host/db"),
            "postgresql+psycopg://u:p@host/db",
        )
        self.assertEqual(
            db_mod._normalize_database_url("postgres://u:p@host/db"),
            "postgresql+psycopg://u:p@host/db",
        )
        self.assertEqual(
            db_mod._normalize_database_url("postgresql+asyncpg://u:p@host/db"),
            "postgresql+psycopg://u:p@host/db",
        )

    async def test_initialize_postgres_skips_non_postgres_dialect(self):
        fake_conn = AsyncMock()
        fake_conn.dialect.name = "sqlite"

        class _Begin:
            async def __aenter__(self):
                return fake_conn

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_engine = type("E", (), {"begin": lambda self: _Begin()})()
        await db_mod.initialize_postgres(target_engine=fake_engine)

        fake_conn.execute.assert_not_awaited()

    async def test_initialize_postgres_executes_extension_setup(self):
        fake_conn = AsyncMock()
        fake_conn.dialect.name = "postgresql"

        class _VersionResult:
            def scalar_one(self):
                return "160004"

        class _Result:
            pass

        fake_conn.execute.side_effect = [
            _VersionResult(),
            _Result(),
            _Result(),
            _Result(),
        ]

        class _Begin:
            async def __aenter__(self):
                return fake_conn

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_engine = type("E", (), {"begin": lambda self: _Begin()})()
        await db_mod.initialize_postgres(target_engine=fake_engine)

        self.assertEqual(fake_conn.execute.await_count, 4)
        sql_calls = [str(call.args[0]) for call in fake_conn.execute.await_args_list]
        self.assertEqual(sql_calls[0], "SHOW server_version_num")
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", sql_calls[1])
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm", sql_calls[2])
        self.assertIn("CREATE EXTENSION IF NOT EXISTS unaccent", sql_calls[3])


if __name__ == "__main__":
    unittest.main()
