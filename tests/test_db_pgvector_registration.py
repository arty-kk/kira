import unittest

from pgvector.psycopg import Vector as PgVector
from sqlalchemy import text

from app.core import db as core_db


class PgvectorRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def test_register_pgvector_adapters_uses_run_async_for_async_psycopg(self):
        captured = {"called": False, "fn": None}

        class _FakeDbapiConn:
            def run_async(self, fn):
                captured["called"] = True
                captured["fn"] = fn

        core_db._register_pgvector_adapters(_FakeDbapiConn())

        self.assertTrue(captured["called"])
        self.assertIs(captured["fn"], core_db.register_vector_async)

    def test_register_pgvector_adapters_falls_back_to_sync_register(self):
        captured = {"called": False, "conn": None}

        class _FakeDbapiConn:
            pass

        original = core_db.register_vector
        try:
            def _fake_register(conn):
                captured["called"] = True
                captured["conn"] = conn

            core_db.register_vector = _fake_register
            conn = _FakeDbapiConn()
            core_db._register_pgvector_adapters(conn)
        finally:
            core_db.register_vector = original

        self.assertTrue(captured["called"])
        self.assertIsInstance(captured["conn"], _FakeDbapiConn)


    def test_on_connect_marks_connection_record_and_skips_repeat_registration(self):
        captured = {"calls": 0}

        _FakeConn = type("_FakeConn", (), {"__module__": "psycopg.fake"})

        class _FakeRecord:
            def __init__(self):
                self.info = {}

        original = core_db._register_pgvector_adapters
        try:
            def _fake_register(_conn):
                captured["calls"] += 1

            core_db._register_pgvector_adapters = _fake_register
            rec = _FakeRecord()
            conn = _FakeConn()

            core_db._on_connect_register_pgvector(conn, rec)
            core_db._on_connect_register_pgvector(conn, rec)
        finally:
            core_db._register_pgvector_adapters = original

        self.assertEqual(captured["calls"], 1)
        self.assertTrue(rec.info.get("pgvector_registered"))

    async def test_smoke_real_connection_vector_bind_and_distance(self):
        try:
            async with core_db.engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT (:q <=> CAST('[1,0]' AS vector(2))) AS d"),
                        {"q": PgVector([1.0, 0.0])},
                    )
                ).first()
        except Exception as exc:
            self.skipTest(f"real pgvector smoke unavailable: {type(exc).__name__}: {exc}")
            return

        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row[0]), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
