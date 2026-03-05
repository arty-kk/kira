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

from app.services.responder.core import _enqueue_gender_detection


class ResponderGenderEnqueueTests(unittest.TestCase):
    def test_enqueue_calls_detect_gender_task_delay(self) -> None:
        with patch("app.tasks.gender.detect_gender_task.delay") as delay_mock:
            _enqueue_gender_detection(42, " Alex ", "hello")

        delay_mock.assert_called_once_with(user_id=42, name="Alex", text="hello")

    def test_enqueue_skips_when_name_is_empty(self) -> None:
        with patch("app.tasks.gender.detect_gender_task.delay") as delay_mock:
            _enqueue_gender_detection(42, "   ", "hello")

        delay_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
