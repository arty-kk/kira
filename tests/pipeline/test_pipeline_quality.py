import asyncio
import os
import types
import unittest
from unittest import mock


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
    os.environ.setdefault("TWITTER_API_KEY", "test")
    os.environ.setdefault("TWITTER_API_SECRET", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
    os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")
    os.environ.setdefault("API_RATELIMIT_PER_MIN", "1")
    os.environ.setdefault("API_RATELIMIT_BURST_FACTOR", "1")
    os.environ.setdefault("API_RATELIMIT_PER_IP_PER_MIN", "1")


_seed_env()

from fastapi import HTTPException
from pydantic import ValidationError

import app.api.conversation as conversation
from app.api.conversation import ConversationRequest, _check_rate_limit
from app.services.responder import core as responder_core
from app.emo_engine.persona.stylers import modifiers as style_mods


class FakeRedis:
    def __init__(self) -> None:
        self.store = {}

    async def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None


class PipelineQualityTests(unittest.TestCase):
    def test_memory_recall_normalization(self) -> None:
        left = responder_core._norm_cmp_text("Hello!!")
        right = responder_core._norm_cmp_text("hello")
        self.assertEqual(left, right)

    def test_style_adherence_step_cap(self) -> None:
        capped = style_mods._apply_step_cap(prev_val=0.1, candidate=0.9, max_step=0.2)
        self.assertAlmostEqual(capped, 0.3)

    def test_voice_to_text_payload_accepts_voice_only(self) -> None:
        req = ConversationRequest(
            user_id="u1",
            voice_b64="aGVsbG8=",
            voice_mime="audio/ogg",
        )
        self.assertEqual(req.voice_b64, "aGVsbG8=")

    def test_invalid_payload_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            ConversationRequest(user_id="u1")

    def test_rate_limit_blocks_on_second_call(self) -> None:
        request = types.SimpleNamespace(
            headers={},
            client=types.SimpleNamespace(host="1.2.3.4"),
        )
        fake_redis = FakeRedis()
        with mock.patch.object(conversation, "get_redis", return_value=fake_redis):
            asyncio.run(_check_rate_limit(request, api_key_id=1))
            with self.assertRaises(HTTPException):
                asyncio.run(_check_rate_limit(request, api_key_id=1))

    def test_long_context_rejected(self) -> None:
        long_text = "a" * 4001
        with self.assertRaises(ValidationError):
            ConversationRequest(user_id="u1", message=long_text)

    def test_safety_edge_strips_openai_utm_links(self) -> None:
        raw = "See (https://example.com?utm_source=openai&utm_medium=web)."
        cleaned = responder_core._drop_openai_utm_links(raw)
        self.assertNotIn("utm_source=openai", cleaned)


if __name__ == "__main__":
    unittest.main()
