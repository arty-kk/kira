# ruff: noqa: E402
import asyncio
import contextlib
import os
import unittest


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

from app.emo_engine.persona.core import Persona


class PersonaBackgroundStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_background_started_tracks_failed_then_successful_start(self) -> None:
        persona = Persona(chat_id=7001)
        spawn_calls: list[tuple] = []
        spawned_notify_tasks: list[asyncio.Task] = []

        def _fake_spawn_coro(coro_fn, *args, **kwargs):
            spawn_calls.append((coro_fn, args, kwargs))
            task = asyncio.create_task(asyncio.sleep(0), name="test-notify-ready")
            spawned_notify_tasks.append(task)
            return task

        persona.spawn_coro = _fake_spawn_coro  # type: ignore[method-assign]

        persona._bg_queue = None
        await persona._ensure_background_started()

        self.assertFalse(persona._bg_started)
        self.assertEqual(spawn_calls, [])
        self.assertIsNone(persona._worker_task)

        persona._bg_queue = asyncio.Queue(maxsize=1)
        await persona._ensure_background_started()

        self.assertIsNotNone(persona._worker_task)
        self.assertFalse(persona._worker_task.done())
        self.assertTrue(persona._bg_started)
        self.assertEqual(len(spawn_calls), 1)
        self.assertEqual(spawn_calls[0][0], persona._notify_ready)

        persona._bg_stop = True
        if persona._worker_task is not None:
            persona._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await persona._worker_task

        for task in spawned_notify_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
