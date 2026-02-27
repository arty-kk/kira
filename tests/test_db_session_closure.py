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

    async def test_session_scope_fails_fast_when_read_only_set_local_fails(self):
        fake = _FakeSession([True, False, False])

        async def _execute_side_effect(statement):
            if str(statement) == "SET LOCAL default_transaction_read_only = on":
                raise RuntimeError("readonly setup failed")

        fake.execute.side_effect = _execute_side_effect
        user_block_executed = False

        with patch.object(db_mod, "AsyncSessionLocal", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "readonly setup failed"):
                async with db_mod.session_scope(read_only=True):
                    user_block_executed = True

        self.assertFalse(user_block_executed)
        fake.commit.assert_not_awaited()
        fake.close.assert_awaited_once()


    async def test_session_scope_fails_fast_when_lock_timeout_set_local_fails(self):
        fake = _FakeSession([True, False, False])

        async def _execute_side_effect(statement):
            if str(statement) == "SET LOCAL lock_timeout = 1000":
                raise RuntimeError("lock timeout setup failed")

        fake.execute.side_effect = _execute_side_effect
        user_block_executed = False

        with patch.object(db_mod, "AsyncSessionLocal", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "lock timeout setup failed"):
                async with db_mod.session_scope(stmt_timeout_ms=1500, read_only=False):
                    user_block_executed = True

        self.assertFalse(user_block_executed)
        fake.commit.assert_not_awaited()
        fake.close.assert_awaited_once()

    async def test_session_scope_fails_fast_when_stmt_timeout_set_local_fails(self):
        fake = _FakeSession([True, False, False])

        async def _execute_side_effect(statement):
            if str(statement) == "SET LOCAL statement_timeout = 1500":
                raise RuntimeError("stmt timeout setup failed")

        fake.execute.side_effect = _execute_side_effect
        user_block_executed = False

        with patch.object(db_mod, "AsyncSessionLocal", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "stmt timeout setup failed"):
                async with db_mod.session_scope(stmt_timeout_ms=1500, read_only=False):
                    user_block_executed = True

        self.assertFalse(user_block_executed)
        fake.commit.assert_not_awaited()
        fake.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
