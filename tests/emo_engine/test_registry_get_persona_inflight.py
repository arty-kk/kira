import asyncio
import os
import unittest
from contextlib import asynccontextmanager
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

from app.emo_engine import registry


class _DummyDB:
    async def get(self, *_args, **_kwargs):
        return None


class RegistryInflightFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await registry.shutdown_personas()

    async def asyncTearDown(self) -> None:
        await registry.shutdown_personas()

    async def test_waiter_gets_creator_failure_and_retry_works(self) -> None:
        failure = RuntimeError("session_scope failed after Persona init")
        creator_entered = asyncio.Event()
        waiter_started = asyncio.Event()
        first_call = {"value": True}

        @asynccontextmanager
        async def controlled_session_scope(*_args, **_kwargs):
            if first_call["value"]:
                first_call["value"] = False
                creator_entered.set()
                await asyncio.wait_for(waiter_started.wait(), timeout=1)
                raise failure
            yield _DummyDB()

        key = (1001, 0, 0, "")

        with patch("app.emo_engine.registry.session_scope", new=controlled_session_scope):
            creator_task = asyncio.create_task(registry.get_persona(chat_id=1001))
            await asyncio.wait_for(creator_entered.wait(), timeout=1)

            waiter_task = asyncio.create_task(registry.get_persona(chat_id=1001))
            waiter_started.set()

            with self.assertRaises(RuntimeError) as creator_err:
                await asyncio.wait_for(creator_task, timeout=2)
            with self.assertRaises(RuntimeError) as waiter_err:
                await asyncio.wait_for(waiter_task, timeout=2)

            self.assertEqual(str(creator_err.exception), str(failure))
            self.assertEqual(str(waiter_err.exception), str(failure))

            async with registry._lock:
                self.assertNotIn(key, registry._inflight)

            persona = await asyncio.wait_for(registry.get_persona(chat_id=1001), timeout=2)
            self.assertIsNotNone(persona)


if __name__ == "__main__":
    unittest.main()
