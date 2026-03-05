import asyncio
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

from app.tasks.gender import detect_gender_task


class GenderTaskTests(unittest.TestCase):
    def test_detect_gender_task_rejects_bool_user_id(self) -> None:
        result = detect_gender_task._orig_run(user_id=True, name="Alex", text="hello")
        self.assertEqual(result, "invalid_payload")

    def test_detect_gender_task_returns_skip_for_empty_name(self) -> None:
        result = detect_gender_task._orig_run(user_id=42, name="   ", text="hello")
        self.assertEqual(result, "skip")

    def test_detect_gender_task_caches_detected_gender(self) -> None:
        calls = {"count": 0}

        async def _detect_stub(name: str, text: str) -> str:
            self.assertEqual(name, "Alex")
            self.assertEqual(text, "hello")
            return "male"

        async def _cache_stub(user_id: int, gender: str) -> None:
            self.assertEqual(user_id, 42)
            self.assertEqual(gender, "male")

        def _run_sync_stub(coro, timeout=None):
            calls["count"] += 1
            return asyncio.run(coro)

        task_globals = detect_gender_task._orig_run.__func__.__globals__
        with patch.dict(task_globals, {"run_coro_sync": _run_sync_stub, "detect_gender": _detect_stub, "cache_gender": _cache_stub}):
            result = detect_gender_task._orig_run(user_id=42, name=" Alex ", text="hello")

        self.assertEqual(result, "cached")
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
