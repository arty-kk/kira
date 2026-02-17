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

from app.bot.handlers import moderation
from app.services.addons import passive_moderation


class PassiveModerationSourceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_light_channel_skips_user_only_heuristics(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=True)) as flooding_mock,
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=[])),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=True)) as toxic_mock,
        ):
            status = await passive_moderation.check_light(1, 2, "hello", [], source="channel")

        self.assertEqual(status, "clean")
        flooding_mock.assert_not_awaited()
        toxic_mock.assert_not_awaited()


    async def test_check_light_user_keeps_flood_behavior(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=True)) as flooding_mock,
        ):
            status = await passive_moderation.check_light(1, 2, "hello", [], source="user")

        self.assertEqual(status, "flood")
        flooding_mock.assert_awaited_once()

    async def test_check_deep_channel_skips_history_and_ai(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "load_context", AsyncMock(return_value=[{"role": "user", "content": "x"}])) as load_mock,
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=True)) as ai_mock,
        ):
            blocked = await passive_moderation.check_deep(1, 2, "hello", source="channel")

        self.assertFalse(blocked)
        load_mock.assert_not_awaited()
        ai_mock.assert_not_awaited()



    async def test_check_deep_bot_skips_history_and_ai(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "load_context", AsyncMock(return_value=[{"role": "user", "content": "x"}])) as load_mock,
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=True)) as ai_mock,
        ):
            blocked = await passive_moderation.check_deep(1, 2, "hello", source="bot")

        self.assertFalse(blocked)
        load_mock.assert_not_awaited()
        ai_mock.assert_not_awaited()

class _FakePipe:
    def set(self, *args, **kwargs):
        return None

    async def execute(self):
        return [True]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedis:
    def __init__(self) -> None:
        self.hset = AsyncMock()
        self.sadd = AsyncMock()
        self.set = AsyncMock()
        self.zrem = AsyncMock()

    def pipeline(self, transaction=True):
        return _FakePipe()


class ModerationHandlerSourceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_passive_moderation_passes_channel_source_as_is(self) -> None:
        fake_redis = _FakeRedis()
        check_light_mock = AsyncMock(return_value="clean")
        check_deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", check_deep_mock),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="hello",
                entities=[],
                image_b64="aGVsbG8=",
                image_mime="image/jpeg",
                source="channel",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "clean")
        self.assertEqual(check_light_mock.await_args.kwargs["source"], "channel")
        self.assertEqual(check_deep_mock.await_args.kwargs["source"], "channel")


if __name__ == "__main__":
    unittest.main()
