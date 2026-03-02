# ruff: noqa: E402
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import redis.exceptions


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
    os.environ.setdefault("TWITTER_API_KEY", "test")
    os.environ.setdefault("TWITTER_API_SECRET", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
    os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")


_seed_env()

from app.emo_engine.persona import memory


class _RedisGetError:
    async def get(self, _key):
        raise redis.exceptions.RedisError("redis unavailable")


class _RedisGenericError:
    async def get(self, _key):
        raise RuntimeError("boom")


class GetEmbeddingRedisFailOpenTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _build_unit_embedding_bytes() -> bytes:
        vec = [0.0] * memory._DIM
        vec[0] = 3.0
        vec[1] = 4.0
        arr = memory.np.asarray(vec, dtype=memory.np.float32)
        arr = arr / float(memory.np.linalg.norm(arr))
        return arr.tobytes()

    async def test_get_embedding_with_no_redis_returns_non_zero_normalized_vector(self) -> None:
        expected = self._build_unit_embedding_bytes()

        async def fake_embedding_call(**_kwargs):
            vec = [0.0] * memory._DIM
            vec[0] = 3.0
            vec[1] = 4.0
            return SimpleNamespace(data=[SimpleNamespace(embedding=vec)])

        with patch.object(memory, "_BG_BATCH_MAX", 1), patch.object(memory, "get_redis", return_value=None), patch.object(
            memory,
            "_call_openai_with_retry",
            side_effect=fake_embedding_call,
        ):
            result = await memory.get_embedding("hello redis fail-open")

        self.assertEqual(result, expected)
        self.assertNotEqual(result, memory._ZERO_VEC)

    async def test_get_embedding_redis_error_on_get_falls_back_to_openai(self) -> None:
        expected = self._build_unit_embedding_bytes()

        async def fake_embedding_call(**_kwargs):
            vec = [0.0] * memory._DIM
            vec[0] = 3.0
            vec[1] = 4.0
            return SimpleNamespace(data=[SimpleNamespace(embedding=vec)])

        with patch.object(memory, "_BG_BATCH_MAX", 1), patch.object(memory, "get_redis", return_value=_RedisGetError()), patch.object(
            memory,
            "_call_openai_with_retry",
            side_effect=fake_embedding_call,
        ):
            result = await memory.get_embedding("redis error")

        self.assertEqual(result, expected)

    async def test_get_embedding_generic_error_on_get_falls_back_to_openai(self) -> None:
        expected = self._build_unit_embedding_bytes()

        async def fake_embedding_call(**_kwargs):
            vec = [0.0] * memory._DIM
            vec[0] = 3.0
            vec[1] = 4.0
            return SimpleNamespace(data=[SimpleNamespace(embedding=vec)])

        with patch.object(memory, "_BG_BATCH_MAX", 1), patch.object(memory, "get_redis", return_value=_RedisGenericError()), patch.object(
            memory,
            "_call_openai_with_retry",
            side_effect=fake_embedding_call,
        ):
            result = await memory.get_embedding("generic error")

        self.assertEqual(result, expected)

    async def test_get_embedding_zero_fallback_for_short_text(self) -> None:
        with patch.object(memory, "get_redis", return_value=None), patch.object(memory, "_norm_text_for_embed", return_value=""):
            result = await memory.get_embedding("short")

        self.assertEqual(result, memory._ZERO_VEC)


if __name__ == "__main__":
    unittest.main()
