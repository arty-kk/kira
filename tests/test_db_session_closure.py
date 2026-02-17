import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from app.core import db as db_mod


class _FakeSession:
    def __init__(self, in_transaction_seq):
        self._seq = list(in_transaction_seq)
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.close = AsyncMock()
        self.execute = AsyncMock()

    def in_transaction(self):
        if self._seq:
            return self._seq.pop(0)
        return False


class DbSessionClosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_scope_rolls_back_open_tx_on_exit_when_autocommit_disabled(self):
        fake = _FakeSession([True, True, False])

        @asynccontextmanager
        async def _scope():
            async with db_mod.session_scope(autocommit=False) as session:
                yield session

        with patch.object(db_mod, "AsyncSessionLocal", return_value=fake):
            async with _scope() as session:
                self.assertIs(session, fake)

        fake.commit.assert_not_awaited()
        fake.rollback.assert_awaited_once()
        fake.close.assert_awaited_once()

    async def test_get_db_rolls_back_after_commit_failure_and_closes(self):
        fake = _FakeSession([True, True, False])
        fake.commit.side_effect = RuntimeError("commit failed")

        @asynccontextmanager
        async def _dep():
            async with db_mod.get_db() as session:
                yield session

        with patch.object(db_mod, "AsyncSessionLocal", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                async with _dep() as session:
                    self.assertIs(session, fake)

        fake.commit.assert_awaited_once()
        fake.rollback.assert_awaited_once()
        fake.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
