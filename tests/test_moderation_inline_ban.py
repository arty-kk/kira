import os
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.bot.handlers import moderation


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def hset(self, key: str, mapping: dict) -> None:
        self.hashes[key] = dict(mapping)

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))


class ModerationInlineBanTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_ban_without_trigger_message_creates_unique_audit_entries(self) -> None:
        fake_redis = _FakeRedis()
        callback = types.SimpleNamespace(
            data="mod:ban:-100500:777",
            from_user=types.SimpleNamespace(id=42),
            message=None,
            answer=AsyncMock(),
        )

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "_ban_user_safe", AsyncMock(return_value=True)),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_BAN_REVOKE_MESSAGES=True, NEW_USER_TTL_SECONDS=120)),
        ):
            await moderation.moderation_inline_ban(callback)
            await moderation.moderation_inline_ban(callback)

        keys = list(fake_redis.hashes.keys())
        self.assertEqual(2, len(keys))
        self.assertTrue(all(key.startswith("mod:combot:inline:-100500:") for key in keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("mod:combot:-100500:0", keys)

        for key in keys:
            payload = fake_redis.hashes[key]
            self.assertEqual("ban", payload["action"])
            self.assertEqual("inline_button", payload["reason"])
            self.assertEqual(777, payload["user_id"])
            self.assertIsInstance(payload["ts"], int)

        self.assertEqual(sorted(keys), sorted(k for k, _ in fake_redis.expire_calls))


if __name__ == "__main__":
    unittest.main()
