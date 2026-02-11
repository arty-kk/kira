import asyncio
import os
import uuid
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


class _DummyPersona:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id

    def apply_overrides(self, *_args, **_kwargs):
        return None

    async def close(self):
        return None


class RegistryInflightFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await registry.shutdown_personas()

    async def asyncTearDown(self) -> None:
        await registry.shutdown_personas()

    async def test_waiter_gets_creator_failure_and_retry_works(self) -> None:
        failure = RuntimeError("session_scope failed after Persona init")
        chat_id = 987654321
        profile_id = f"inflight-{uuid.uuid4()}"
        allow_success = asyncio.Event()

        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

        @asynccontextmanager
        async def controlled_session_scope(*_args, **_kwargs):
            if not allow_success.is_set():
                await asyncio.sleep(0.05)
                raise failure
            yield _DummyDB()

        key = (chat_id, 0, 0, profile_id)

        with patch.object(registry, "session_scope", new=controlled_session_scope), patch.object(
            registry,
            "Persona",
            new=_DummyPersona,
        ):
            with self.assertRaises(RuntimeError) as first_err:
                await asyncio.wait_for(
                    registry.get_persona(chat_id=chat_id, profile_id=profile_id),
                    timeout=2,
                )
            self.assertEqual(str(first_err.exception), str(failure))

            async with registry._lock:
                self.assertNotIn(key, registry._inflight)

            allow_success.set()
            creator_task = asyncio.create_task(registry.get_persona(chat_id=chat_id, profile_id=profile_id))
            await asyncio.sleep(0)
            waiter_task = asyncio.create_task(registry.get_persona(chat_id=chat_id, profile_id=profile_id))

            creator_persona = await asyncio.wait_for(creator_task, timeout=2)
            waiter_persona = await asyncio.wait_for(waiter_task, timeout=2)

            self.assertIsNotNone(creator_persona)
            self.assertIs(creator_persona, waiter_persona)


if __name__ == "__main__":
    unittest.main()
