import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/3")

from app.config import settings
from app.tasks import celery_app
from app.tasks import welcome


class AsyncioExecutionModelTests(unittest.TestCase):
    def test_celery_app_has_no_bridge_run_helper(self) -> None:
        self.assertFalse(hasattr(celery_app, "_run"))

    def test_run_coro_sync_uses_default_timeout(self) -> None:
        captured = {}

        async def _sample() -> str:
            return "ok"

        async def _fake_wait_for(coro, timeout):
            captured["timeout"] = timeout
            return await coro

        with patch.object(celery_app.asyncio, "wait_for", side_effect=_fake_wait_for):
            result = celery_app.run_coro_sync(_sample())

        self.assertEqual(result, "ok")
        self.assertEqual(captured["timeout"], settings.CELERY_RUN_TIMEOUT_SEC)

    def test_welcome_task_uses_run_coro_sync_with_task_timeout(self) -> None:
        payload = {"chat_id": 1, "user_id": 1, "username": "u"}
        def _fake_runner(coro, *, timeout=None):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return None

        with patch.object(welcome, "run_coro_sync", side_effect=_fake_runner) as runner_mock:
            welcome.send_group_welcome_task.run(1, payload)

        runner_mock.assert_called_once()
        self.assertEqual(runner_mock.call_args.kwargs["timeout"], welcome.WELCOME_GROUP_RUN_TIMEOUT_SEC)


if __name__ == "__main__":
    unittest.main()
