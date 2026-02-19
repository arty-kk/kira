import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy.dialects import postgresql

from app.core.models import User
from app.services.user.user_service import get_or_create_user


class GetOrCreateUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_or_create_user_uses_single_upsert_roundtrip_and_no_db_get(self):
        tg_user = SimpleNamespace(id=42, username="neo", full_name="Thomas Anderson")
        expected_user = User(id=tg_user.id, username=tg_user.username, full_name=tg_user.full_name)

        result = Mock()
        result.scalar_one.return_value = expected_user

        db = AsyncMock()
        db.execute.return_value = result

        actual_user = await get_or_create_user(db, tg_user)

        self.assertIs(actual_user, expected_user)
        db.execute.assert_awaited_once()
        db.get.assert_not_awaited()

        stmt = db.execute.await_args.args[0]
        sql = str(stmt)
        self.assertIn("INSERT INTO users", sql)
        self.assertIn("ON CONFLICT", sql)
        self.assertIn("RETURNING users.id", sql)
        self.assertIn("users.free_requests", sql)
        self.assertIn("users.paid_requests", sql)
        compiled = stmt.compile(dialect=postgresql.dialect())
        self.assertEqual(compiled.params["free_requests"], 20)
        self.assertEqual(compiled.params["paid_requests"], 0)
        self.assertEqual(compiled.params["used_requests"], 0)


if __name__ == "__main__":
    unittest.main()
