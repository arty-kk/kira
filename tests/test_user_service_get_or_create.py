import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sqlalchemy.dialects import postgresql

from app.core.models import User
from app.services.user.user_service import INITIAL_FREE_REQUESTS, get_or_create_user


class GetOrCreateUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_or_create_user_falls_back_to_select_when_returning_scalar_is_int(self):
        tg_user = SimpleNamespace(id=55, username="switch", full_name="Switch")
        expected_user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            used_requests=1,
            free_requests=2,
            paid_requests=0,
        )

        upsert_result = Mock()
        upsert_result.scalar_one.return_value = tg_user.id
        select_result = Mock()
        select_result.scalar_one.return_value = expected_user

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[upsert_result, select_result])

        actual_user = await get_or_create_user(db, tg_user)

        self.assertIs(actual_user, expected_user)
        self.assertEqual(db.execute.await_count, 2)
        fallback_stmt = db.execute.await_args_list[1].args[0]
        self.assertIn("SELECT", str(fallback_stmt))

    async def test_get_or_create_user_fallback_int_keeps_zero_triplet_heal(self):
        tg_user = SimpleNamespace(id=56, username="dozer", full_name="Dozer")
        expected_user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            used_requests=0,
            free_requests=0,
            paid_requests=0,
        )

        upsert_result = Mock()
        upsert_result.scalar_one.return_value = tg_user.id
        select_result = Mock()
        select_result.scalar_one.return_value = expected_user
        heal_result = Mock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[upsert_result, select_result, heal_result])

        actual_user = await get_or_create_user(db, tg_user)

        self.assertIs(actual_user, expected_user)
        self.assertEqual(db.execute.await_count, 3)
        self.assertEqual(actual_user.free_requests, INITIAL_FREE_REQUESTS)

    async def test_get_or_create_user_uses_upsert_and_no_db_get(self):
        tg_user = SimpleNamespace(id=42, username="neo", full_name="Thomas Anderson")
        expected_user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            used_requests=1,
            free_requests=2,
            paid_requests=0,
        )

        result = Mock()
        result.scalar_one.return_value = expected_user

        db = AsyncMock()
        db.execute.return_value = result

        actual_user = await get_or_create_user(db, tg_user)

        self.assertIs(actual_user, expected_user)
        self.assertEqual(db.execute.await_count, 1)
        db.get.assert_not_awaited()

        stmt = db.execute.await_args_list[0].args[0]
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

    async def test_get_or_create_user_self_heals_zero_triplet(self):
        tg_user = SimpleNamespace(id=43, username="trinity", full_name="Trinity")
        expected_user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            used_requests=0,
            free_requests=0,
            paid_requests=0,
        )

        first_result = Mock()
        first_result.scalar_one.return_value = expected_user
        second_result = Mock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[first_result, second_result])

        actual_user = await get_or_create_user(db, tg_user)

        self.assertIs(actual_user, expected_user)
        self.assertEqual(db.execute.await_count, 2)
        db.get.assert_not_awaited()
        self.assertEqual(actual_user.free_requests, INITIAL_FREE_REQUESTS)

        heal_stmt = db.execute.await_args_list[1].args[0]
        heal_sql = str(heal_stmt)
        self.assertIn("UPDATE users", heal_sql)
        self.assertIn("users.used_requests =", heal_sql)
        self.assertIn("users.free_requests =", heal_sql)
        self.assertIn("users.paid_requests =", heal_sql)
        self.assertEqual(heal_stmt.compile(dialect=postgresql.dialect()).params["free_requests"], INITIAL_FREE_REQUESTS)

    async def test_get_or_create_user_does_not_heal_when_used_or_balance_nonzero(self):
        tg_user = SimpleNamespace(id=44, username="morpheus", full_name="Morpheus")

        for expected_user in (
            User(id=44, username="morpheus", full_name="Morpheus", used_requests=1, free_requests=0, paid_requests=0),
            User(id=44, username="morpheus", full_name="Morpheus", used_requests=0, free_requests=1, paid_requests=0),
            User(id=44, username="morpheus", full_name="Morpheus", used_requests=0, free_requests=0, paid_requests=1),
        ):
            result = Mock()
            result.scalar_one.return_value = expected_user
            db = AsyncMock()
            db.execute.return_value = result

            actual_user = await get_or_create_user(db, tg_user)

            self.assertIs(actual_user, expected_user)
            self.assertEqual(db.execute.await_count, 1)
            db.get.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
