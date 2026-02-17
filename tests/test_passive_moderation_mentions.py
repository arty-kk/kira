import os
import asyncio
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


class _FakeRedisMentions:
    def __init__(self) -> None:
        self.cache: dict[str, str] = {}

    async def hget(self, key: str, field: str):
        return None

    async def get(self, key: str):
        return self.cache.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.cache[key] = value
        return True


class _FakePipe:
    def set(self, *args, **kwargs):
        return None

    async def execute(self):
        return [True]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedisHandler:
    def __init__(self) -> None:
        self.hset = AsyncMock()
        self.sadd = AsyncMock()
        self.set = AsyncMock()
        self.zrem = AsyncMock()

    def pipeline(self, transaction=True):
        return _FakePipe()


class PassiveModerationMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_light_returns_spam_mentions_before_resolve(self) -> None:
        text = "@a @b @c @d"
        entities = [
            {"type": "mention", "offset": 0, "length": 2},
            {"type": "mention", "offset": 3, "length": 2},
            {"type": "mention", "offset": 6, "length": 2},
            {"type": "mention", "offset": 9, "length": 2},
        ]
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=2)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=[])) as mentions_mock,
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "spam_mentions")
        mentions_mock.assert_not_awaited()

    async def test_extract_external_mentions_caches_resolution(self) -> None:
        redis = _FakeRedisMentions()
        bot = types.SimpleNamespace(get_chat=AsyncMock(return_value=types.SimpleNamespace(type="private", is_bot=False)))
        text = "hello @Known and @Known"
        entities = [
            {"type": "mention", "offset": 6, "length": 6},
            {"type": "mention", "offset": 17, "length": 6},
        ]

        with (
            patch.object(passive_moderation, "get_redis", return_value=redis),
            patch.object(passive_moderation, "get_bot", return_value=bot),
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
        ):
            first = await passive_moderation.extract_external_mentions(1, text, entities)
            second = await passive_moderation.extract_external_mentions(1, text, entities)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        bot.get_chat.assert_awaited_once_with("@known")

    async def test_handle_passive_moderation_timeout_is_risky_not_clean(self) -> None:
        fake_redis = _FakeRedisHandler()

        async def _slow_light(*args, **kwargs):
            await asyncio.sleep(0.05)
            return "clean"

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=0.01, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", _slow_light),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
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
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "flagged")
        self.assertNotEqual(status, "clean")
        self.assertEqual(fake_redis.hset.await_args.kwargs["mapping"]["reason"], "Light moderation timeout (risk fallback)")


if __name__ == "__main__":
    unittest.main()
