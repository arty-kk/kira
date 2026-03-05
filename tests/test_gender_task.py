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
        result = detect_gender_task(user_id=True, name="Alex", text="hello")
        self.assertEqual(result, "invalid_payload")

    def test_detect_gender_task_returns_skip_for_empty_name(self) -> None:
        result = detect_gender_task(user_id=42, name="   ", text="hello")
        self.assertEqual(result, "skip")

    def test_detect_gender_task_caches_detected_gender(self) -> None:
        calls = {"count": 0}

        def _run_sync_stub(coro, timeout=None):
            calls["count"] += 1
            if hasattr(coro, "close"):
                coro.close()
            if calls["count"] == 1:
                return "male"
            return None

        with (
            patch("app.tasks.gender.run_coro_sync", side_effect=_run_sync_stub) as run_mock,
            patch("app.tasks.gender.cache_gender") as cache_mock,
        ):
            result = detect_gender_task(user_id=42, name=" Alex ", text="hello")

        self.assertEqual(result, "cached")
        self.assertEqual(run_mock.call_count, 2)
        cache_mock.assert_called_once_with(42, "male")


if __name__ == "__main__":
    unittest.main()
