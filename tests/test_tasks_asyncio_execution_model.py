import os
import unittest
from unittest.mock import patch
import concurrent.futures

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

        class _DoneFuture:
            def result(self, timeout=None):
                captured["timeout"] = timeout
                return "ok"

        class _Runner:
            loop = object()

            def submit(self, _coro):
                close = getattr(_coro, "close", None)
                if callable(close):
                    close()
                return _DoneFuture()

        with patch.object(celery_app, "_get_worker_loop_runner", return_value=_Runner()):
            result = celery_app.run_coro_sync(_sample())

        self.assertEqual(result, "ok")
        self.assertEqual(captured["timeout"], settings.CELERY_RUN_TIMEOUT_SEC)

    def test_run_coro_sync_reuses_single_runner_loop_for_multiple_calls(self) -> None:
        captured_loops = []

        async def _sample() -> str:
            return "ok"

        class _DoneFuture:
            def result(self, timeout=None):
                return "ok"

        class _Runner:
            loop = object()

            def submit(self, coro):
                return celery_app.asyncio.run_coroutine_threadsafe(coro, self.loop)

        runner = _Runner()

        def _fake_run_coroutine_threadsafe(_coro, loop):
            captured_loops.append(loop)
            close = getattr(_coro, "close", None)
            if callable(close):
                close()
            return _DoneFuture()

        with patch.object(celery_app, "_get_worker_loop_runner", return_value=runner), patch.object(
            celery_app.asyncio, "run_coroutine_threadsafe", side_effect=_fake_run_coroutine_threadsafe
        ):
            self.assertEqual(celery_app.run_coro_sync(_sample()), "ok")
            self.assertEqual(celery_app.run_coro_sync(_sample()), "ok")

        self.assertEqual(captured_loops, [runner.loop, runner.loop])

    def test_run_coro_sync_timeout_logs_context_and_raises_asyncio_timeout(self) -> None:
        async def _sample() -> str:
            return "ok"

        class _TimeoutFuture:
            def result(self, timeout=None):
                raise concurrent.futures.TimeoutError

            def cancel(self):
                return True

        class _Runner:
            def submit(self, _coro):
                close = getattr(_coro, "close", None)
                if callable(close):
                    close()
                return _TimeoutFuture()

        with patch.object(celery_app, "_get_worker_loop_runner", return_value=_Runner()), patch.object(
            celery_app.logger, "error"
        ) as mock_error:
            with self.assertRaises(celery_app.asyncio.TimeoutError):
                celery_app.run_coro_sync(_sample(), timeout=1)

        self.assertEqual(mock_error.call_count, 1)
        self.assertEqual(mock_error.call_args.kwargs["extra"]["phase"], "run_coro_sync_wait_for")

    def test_worker_shutdown_uses_runner_cleanup_and_stop(self) -> None:
        calls = []

        class _DoneFuture:
            def result(self, timeout=None):
                calls.append(("result", timeout))
                return None

        class _Runner:
            def is_running(self):
                return True

            def submit(self, coro):
                calls.append("submit")
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
                return _DoneFuture()

            def stop(self):
                calls.append("stop")

        runner = _Runner()
        with patch.object(celery_app, "_get_existing_worker_loop_runner", return_value=runner), patch.object(
            celery_app, "_worker_loop_runner", runner
        ):
            celery_app._close_telegram_bot_sessions()

        self.assertIn("submit", calls)
        self.assertIn("stop", calls)

    def test_welcome_task_uses_run_coro_sync_with_task_timeout(self) -> None:
        payload = {"chat_id": 1, "user_id": 1, "username": "u"}
        captured = {"calls": 0}

        def _fake_runner(coro, *, timeout=None):
            captured["calls"] += 1
            captured["timeout"] = timeout
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return None

        with patch.dict(
            welcome.send_group_welcome_task.run.__globals__,
            {"run_coro_sync": _fake_runner},
        ):
            welcome.send_group_welcome_task.run(1, payload)

        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["timeout"], welcome.WELCOME_GROUP_RUN_TIMEOUT_SEC)


if __name__ == "__main__":
    unittest.main()
