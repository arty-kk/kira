import os
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.bot.handlers import private


class _DummySessionScope:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class PrivateBillingAndKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_access_prefers_free_tier_before_paid(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=100),
            from_user=types.SimpleNamespace(id=42),
            message_id=77,
        )
        fake_user = types.SimpleNamespace(id=42, free_requests=3, paid_requests=5)
        fake_reservation = types.SimpleNamespace(reserved=True, used_paid=False, reservation_id=900)
        reserve_mock = AsyncMock(return_value=fake_reservation)

        async def _fake_tr(_uid, _key, default, **_kwargs):
            return default

        def _drop_task(coro):
            coro.close()
            return None

        with (
            patch.object(private, "pm_block_guard", AsyncMock(return_value=False)),
            patch.object(private, "register_private_activity", AsyncMock()),
            patch.object(private.asyncio, "create_task", side_effect=_drop_task),
            patch.object(private, "SOFT_PENDING_INVOICE", False),
            patch.object(private, "redis_client", types.SimpleNamespace(exists=AsyncMock(return_value=False))),
            patch.object(private, "session_scope", side_effect=lambda **_kwargs: _DummySessionScope(object())),
            patch.object(private, "get_or_create_user", AsyncMock(return_value=fake_user)),
            patch.object(private, "compute_remaining", return_value=8),
            patch.object(private, "reserve_request", reserve_mock),
            patch.object(private, "send_message_safe", AsyncMock()),
            patch.object(private, "tr", AsyncMock(side_effect=_fake_tr)),
        ):
            result = await private._ensure_access_and_increment(message, "hello")

        self.assertIsNotNone(result)
        self.assertEqual(reserve_mock.await_args.kwargs["prefer_paid"], False)

    async def test_build_quick_links_keyboard_is_not_persistent(self) -> None:
        settings_stub = types.SimpleNamespace(
            SHOW_SHOP_BUTTON=True,
            SHOW_REQUESTS_BUTTON=True,
            SHOW_CHANNEL_BUTTON=True,
            SHOW_PERSONA_BUTTON=True,
            SHOW_MEMORY_CLEAR_BUTTON=True,
            SHOW_API_BUTTON=True,
        )

        async def _fake_tr(_uid, _key, default, **_kwargs):
            return default

        with (
            patch.object(private, "settings", settings_stub),
            patch.object(private, "tr", AsyncMock(side_effect=_fake_tr)),
        ):
            kb = await private.build_quick_links_kb(42)

        self.assertIsNotNone(kb)
        self.assertEqual(kb.is_persistent, False)


if __name__ == "__main__":
    unittest.main()
