import asyncio
import unittest
from contextlib import asynccontextmanager

from app.api import conversation


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self):
        self.rows = []
        self.lookup_key = None

    async def execute(self, stmt):
        sql = str(stmt)
        if "INSERT INTO refund_outbox" in sql:
            params = stmt.compile().params
            key = str(params["request_id"])
            self.lookup_key = key
            for row in self.rows:
                if row["request_id"] == key:
                    return _Result(None)
            row_id = len(self.rows) + 1
            self.rows.append({"id": row_id, **params})
            return _Result(row_id)
        if "SELECT refund_outbox.id" in sql:
            for row in self.rows:
                if row["request_id"] == self.lookup_key:
                    return _Result(row["id"])
            return _Result(None)
        return _Result(None)


class RefundClaimIdempotencyTests(unittest.TestCase):
    def test_safe_refund_request_claim_is_idempotent(self):
        db = _FakeDB()

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield db

        async def _run_test():
            created_first = await conversation._safe_refund_request(
                10,
                "free",
                request_id="req-1",
                reason="worker_error:invalid_payload",
            )
            created_second = await conversation._safe_refund_request(
                10,
                "free",
                request_id="req-1",
                reason="worker_error:invalid_payload",
            )
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(len(db.rows), 1)

        with unittest.mock.patch.object(conversation, "session_scope", _fake_session_scope):
            asyncio.run(_run_test())

    def test_safe_refund_request_deduplicates_by_request_id_with_different_reason(self):
        db = _FakeDB()

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield db

        async def _run_test():
            created_first = await conversation._safe_refund_request(
                10,
                "free",
                request_id="req-1",
                reason="worker_error:invalid_payload",
            )
            created_second = await conversation._safe_refund_request(
                10,
                "free",
                request_id="req-1",
                reason="worker_error:timeout",
            )
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(len(db.rows), 1)

        with unittest.mock.patch.object(conversation, "session_scope", _fake_session_scope):
            asyncio.run(_run_test())


if __name__ == "__main__":
    unittest.main()
