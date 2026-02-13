import asyncio
import unittest

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import MultipleResultsFound

from app.api import api_keys


class _FakeGetKeyResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        raise MultipleResultsFound("must not be used when multiple rows exist")

    def scalars(self):
        class _Scalars:
            def __init__(self, rows):
                self._rows = rows

            def first(self):
                return self._rows[0] if self._rows else None

        return _Scalars(self._rows)


class _FakeGetKeyDb:
    def __init__(self, result_rows):
        self._result_rows = list(result_rows)
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        return _FakeGetKeyResult(self._result_rows)


class ApiKeysGetKeyForUserTests(unittest.TestCase):
    def test_get_key_for_user_returns_latest_active_key(self) -> None:
        active_older = api_keys.ApiKey(id=21, user_id=7, key_hash="k-active", active=True)
        inactive_newer = api_keys.ApiKey(id=22, user_id=7, key_hash="k-inactive", active=False)
        db = _FakeGetKeyDb([active_older, inactive_newer])

        result = asyncio.run(api_keys.get_key_for_user(db, user_id=7))

        self.assertIsNotNone(result)
        self.assertEqual(result.id, 21)
        self.assertEqual(result.key_hash, "k-active")

        self.assertIsNotNone(db.last_stmt)
        compiled = str(
            db.last_stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()
        self.assertIn("from api_keys", compiled)
        self.assertIn("where api_keys.user_id = 7", compiled)
        self.assertIn("api_keys.active is true", compiled)
        self.assertIn("order by api_keys.created_at desc, api_keys.id desc", compiled)
        self.assertIn("limit 1", compiled)


if __name__ == "__main__":
    unittest.main()
