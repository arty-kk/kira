# ruff: noqa: E402
import os
import unittest


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

from app.emo_engine.persona.memory import _to_bool_flag


class MemoryRerankFlagTests(unittest.TestCase):
    def test_to_bool_flag_supported_values(self) -> None:
        cases = [
            (True, True),
            (False, False),
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_to_bool_flag(value, default=False), expected)

    def test_to_bool_flag_none_returns_default(self) -> None:
        self.assertTrue(_to_bool_flag(None, default=True))
        self.assertFalse(_to_bool_flag(None, default=False))


if __name__ == "__main__":
    unittest.main()
