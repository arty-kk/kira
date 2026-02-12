import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/3")
os.environ.setdefault("TWITTER_API_KEY", "x")
os.environ.setdefault("TWITTER_API_SECRET", "x")
os.environ.setdefault("TWITTER_ACCESS_TOKEN", "x")
os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "x")
os.environ.setdefault("TWITTER_BEARER_TOKEN", "x")

from app.config import settings
from app.tasks.celery_app import celery
from app.tasks.moderation import passive_moderate, prepare_moderation_payload


class ModerationCeleryConfigTests(unittest.TestCase):
    def test_task_routes_include_moderation_queue(self) -> None:
        routes = celery.conf.task_routes or {}
        self.assertIn("moderation.*", routes)
        self.assertEqual(routes["moderation.*"]["queue"], settings.CELERY_MODERATION_QUEUE)
        self.assertEqual(routes["media.preprocess_group_image"]["queue"], settings.CELERY_MEDIA_QUEUE)

    def test_passive_moderate_retry_and_limits(self) -> None:
        self.assertEqual(passive_moderate.max_retries, 3)
        self.assertEqual(passive_moderate.soft_time_limit, settings.MODERATION_TIMEOUT)
        self.assertEqual(passive_moderate.time_limit, settings.MODERATION_TIMEOUT + 5)
        self.assertTrue(passive_moderate.retry_backoff)
        self.assertTrue(passive_moderate.retry_jitter)

    def test_prepare_moderation_payload_drops_oversized_json(self) -> None:
        oversized = {"text": "x", "image_b64": "a" * (settings.CELERY_MODERATION_MAX_PAYLOAD_BYTES + 128)}
        prepared = prepare_moderation_payload(oversized, context="test")
        self.assertNotIn("image_b64", prepared)

    def test_prepare_moderation_payload_drops_invalid_base64_and_logs_reason(self) -> None:
        payload = {
            "text": "x",
            "image_b64": "aGVs*bG8=",
            "image_mime": "image/png",
        }
        with self.assertLogs("app.tasks.moderation", level="WARNING") as logs:
            prepared = prepare_moderation_payload(payload, context="api")

        self.assertNotIn("image_b64", prepared)
        self.assertNotIn("image_mime", prepared)
        self.assertTrue(any("invalid base64" in entry and "api" in entry for entry in logs.output))


if __name__ == "__main__":
    unittest.main()
