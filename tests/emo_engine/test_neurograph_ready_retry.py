import os
import unittest
from unittest.mock import patch


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


_seed_env()

import app.emo_engine.persona.neurograph as neurograph_module
from app.emo_engine.persona.neurograph import SelfNeuronNetwork


class _RedisOk:
    def __init__(self, raw=None):
        self.raw = raw
        self.get_calls = 0

    async def get(self, _key):
        self.get_calls += 1
        return self.raw

    async def set(self, *_args, **_kwargs):
        return True


class NeurographReadyRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_retries_after_failure_and_observe_recovers(self) -> None:
        net = SelfNeuronNetwork(chat_id=42)
        net._ready_retry_backoff_sec = 0.0
        redis_ok = _RedisOk(raw=None)

        with patch.object(
            neurograph_module, "get_redis_vector",
            side_effect=[RuntimeError("redis down"), redis_ok],
        ):
            await net.ready()
            self.assertFalse(net._ready)

            metrics = await net.observe(
                uid=7,
                text="hello",
                readings={"arousal": 0.7},
                state={"valence": 0.0, "arousal": 0.6},
                salience=0.8,
            )

        self.assertTrue(net._ready)
        self.assertIn("_mode_id", metrics)
        self.assertEqual(redis_ok.get_calls, 1)

    async def test_ready_backoff_throttles_repeated_init_attempts(self) -> None:
        net = SelfNeuronNetwork(chat_id=77)
        net._ready_retry_backoff_sec = 30.0

        with patch.object(
            neurograph_module, "get_redis_vector",
            side_effect=RuntimeError("redis down"),
        ) as mocked_get_redis:
            await net.ready()
            await net.ready()

        self.assertEqual(mocked_get_redis.call_count, 1)
        self.assertFalse(net._ready)

        net._last_ready_attempt_ts -= (net._ready_retry_backoff_sec + 0.1)
        redis_ok = _RedisOk(raw=None)
        with patch.object(
            neurograph_module, "get_redis_vector",
            return_value=redis_ok,
        ) as mocked_get_redis:
            await net.ready()

        self.assertEqual(mocked_get_redis.call_count, 1)
        self.assertTrue(net._ready)
        self.assertEqual(redis_ok.get_calls, 1)


if __name__ == "__main__":
    unittest.main()
