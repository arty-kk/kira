# ruff: noqa: E402
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


class _TrackedPersona(_DummyPersona):
    created: list["_TrackedPersona"] = []

    def __init__(self, chat_id: int):
        super().__init__(chat_id)
        self.close_calls = 0
        self.__class__.created.append(self)

    async def close(self):
        self.close_calls += 1
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

    async def test_waiter_gets_creator_cancellation_and_inflight_clears(self) -> None:
        chat_id = 246813579
        profile_id = f"inflight-cancel-{uuid.uuid4()}"
        key = (chat_id, 0, 0, profile_id)
        inflight_set = asyncio.Event()
        release_scope = asyncio.Event()

        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

        @asynccontextmanager
        async def controlled_session_scope(*_args, **_kwargs):
            async with registry._lock:
                self.assertIn(key, registry._inflight)
            inflight_set.set()
            await release_scope.wait()
            yield _DummyDB()

        with patch.object(registry, "session_scope", new=controlled_session_scope), patch.object(
            registry,
            "Persona",
            new=_DummyPersona,
        ):
            creator_task = asyncio.create_task(
                registry.get_persona(chat_id=chat_id, profile_id=profile_id)
            )

            await asyncio.wait_for(inflight_set.wait(), timeout=2)

            waiter_task = asyncio.create_task(
                registry.get_persona(chat_id=chat_id, profile_id=profile_id)
            )

            creator_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(creator_task, timeout=2)

            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiter_task, timeout=2)

            async with registry._lock:
                self.assertNotIn(key, registry._inflight)

    async def test_creator_closes_created_persona_on_session_scope_failure(self) -> None:
        failure = RuntimeError("session_scope failed after Persona init")
        chat_id = 1122334455
        profile_id = f"inflight-fail-close-{uuid.uuid4()}"
        key = (chat_id, 0, 0, profile_id)

        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

        _TrackedPersona.created.clear()

        @asynccontextmanager
        async def failing_session_scope(*_args, **_kwargs):
            raise failure
            yield _DummyDB()

        with patch.object(registry, "session_scope", new=failing_session_scope), patch.object(
            registry,
            "Persona",
            new=_TrackedPersona,
        ):
            with self.assertRaises(RuntimeError) as build_err:
                await asyncio.wait_for(
                    registry.get_persona(chat_id=chat_id, profile_id=profile_id),
                    timeout=2,
                )

            self.assertEqual(str(build_err.exception), str(failure))

            async with registry._lock:
                self.assertNotIn(key, registry._inflight)

            self.assertEqual(len(_TrackedPersona.created), 1)
            self.assertEqual(_TrackedPersona.created[0].close_calls, 1)

    async def test_shutdown_during_build_does_not_cache_and_allows_reuse(self) -> None:
        chat_id = 556677889
        profile_id = f"inflight-shutdown-{uuid.uuid4()}"
        key = (chat_id, 0, 0, profile_id)
        build_entered = asyncio.Event()
        release_build = asyncio.Event()
        release_bg = asyncio.Event()

        async with registry._lock:
            registry._cache.clear()
            registry._inflight.clear()

        _TrackedPersona.created.clear()

        bg_task = asyncio.create_task(release_bg.wait())
        async with registry._lock:
            registry._bg_closers.add(bg_task)

        @asynccontextmanager
        async def controlled_session_scope(*_args, **_kwargs):
            build_entered.set()
            await release_build.wait()
            yield _DummyDB()

        with patch.object(registry, "session_scope", new=controlled_session_scope), patch.object(
            registry,
            "Persona",
            new=_TrackedPersona,
        ):
            creator_task = asyncio.create_task(
                registry.get_persona(chat_id=chat_id, profile_id=profile_id)
            )

            await asyncio.wait_for(build_entered.wait(), timeout=2)

            waiter_task = asyncio.create_task(
                registry.get_persona(chat_id=chat_id, profile_id=profile_id)
            )

            shutdown_task = asyncio.create_task(registry.shutdown_personas())
            await asyncio.sleep(0)
            release_build.set()

            with self.assertRaises(RuntimeError) as creator_err:
                await asyncio.wait_for(creator_task, timeout=2)
            with self.assertRaises(RuntimeError) as waiter_err:
                await asyncio.wait_for(waiter_task, timeout=2)

            self.assertEqual(str(creator_err.exception), "persona registry shutdown")
            self.assertEqual(str(waiter_err.exception), "persona registry shutdown")

            release_bg.set()
            await asyncio.wait_for(shutdown_task, timeout=2)

            async with registry._lock:
                self.assertFalse(registry._cache)
                self.assertNotIn(key, registry._inflight)

            self.assertEqual(len(_TrackedPersona.created), 1)
            self.assertEqual(_TrackedPersona.created[0].close_calls, 1)

            fresh = await asyncio.wait_for(
                registry.get_persona(chat_id=chat_id, profile_id=profile_id),
                timeout=2,
            )
            self.assertIsNotNone(fresh)


if __name__ == "__main__":
    unittest.main()
