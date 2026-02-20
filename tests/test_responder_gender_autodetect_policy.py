import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/3")

from app.services.responder.core import _allow_gender_autodetect


class ResponderGenderAutodetectPolicyTests(unittest.TestCase):
    def test_private_chat_allows_autodetect(self):
        self.assertTrue(_allow_gender_autodetect(group_mode=False, is_channel_post=False))

    def test_group_chat_disables_autodetect(self):
        self.assertFalse(_allow_gender_autodetect(group_mode=True, is_channel_post=False))

    def test_channel_post_disables_autodetect(self):
        self.assertFalse(_allow_gender_autodetect(group_mode=False, is_channel_post=True))


if __name__ == "__main__":
    unittest.main()
