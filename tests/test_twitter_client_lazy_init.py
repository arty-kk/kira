import importlib
import os
import sys
import unittest
from contextlib import contextmanager


_TWITTER_ENV_VARS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "TWITTER_BEARER_TOKEN",
)

_REQUIRED_IMPORT_ENV = {
    "OPENAI_API_KEY": "test-openai-key",
    "DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "REDIS_URL_QUEUE": "redis://localhost:6379/1",
    "REDIS_URL_VECTOR": "redis://localhost:6379/2",
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "CELERY_BROKER_URL": "redis://localhost:6379/3",
}


def _drop_module(module_name: str) -> None:
    sys.modules.pop(module_name, None)


def _drop_twitter_related_modules() -> None:
    for name in (
        "app.tasks.periodic",
        "app.services.addons",
        "app.services.addons.twitter_manager",
        "app.clients.twitter_client",
        "app.config",
    ):
        _drop_module(name)


@contextmanager
def _required_app_import_env():
    saved = {k: os.environ.get(k) for k in _REQUIRED_IMPORT_ENV}
    try:
        for key, value in _REQUIRED_IMPORT_ENV.items():
            os.environ[key] = saved[key] or value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TwitterClientLazyInitTests(unittest.IsolatedAsyncioTestCase):
    def test_import_does_not_fail_without_twitter_env(self) -> None:
        saved_env = {key: os.environ.get(key) for key in _TWITTER_ENV_VARS}
        try:
            for key in _TWITTER_ENV_VARS:
                os.environ.pop(key, None)

            _drop_twitter_related_modules()
            with _required_app_import_env():
                importlib.import_module("app.services.addons")
                importlib.import_module("app.tasks.periodic")
        finally:
            _drop_twitter_related_modules()
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_is_twitter_configured_matches_required_env_contract(self) -> None:
        with _required_app_import_env():
            module = importlib.import_module("app.clients.twitter_client")

            original_values = {key: getattr(module.settings, key, None) for key in _TWITTER_ENV_VARS}
            try:
                for key in _TWITTER_ENV_VARS:
                    setattr(module.settings, key, "configured")

                self.assertTrue(module.is_twitter_configured())

                setattr(module.settings, "TWITTER_BEARER_TOKEN", None)
                self.assertFalse(module.is_twitter_configured())
            finally:
                for key, value in original_values.items():
                    setattr(module.settings, key, value)
                module._twitter_client = None
                _drop_twitter_related_modules()

    async def test_post_tweet_raises_config_error_when_creds_missing_and_contract_is_consistent(self) -> None:
        with _required_app_import_env():
            module = importlib.import_module("app.clients.twitter_client")

            original_values = {key: getattr(module.settings, key, None) for key in _TWITTER_ENV_VARS}
            try:
                for key in _TWITTER_ENV_VARS:
                    setattr(module.settings, key, None)
                module._twitter_client = None

                self.assertFalse(module.is_twitter_configured())

                with self.assertRaisesRegex(RuntimeError, "Missing env vars") as ctx:
                    await module.post_tweet("hello")

                for key in _TWITTER_ENV_VARS:
                    self.assertIn(key, str(ctx.exception))
            finally:
                for key, value in original_values.items():
                    setattr(module.settings, key, value)
                module._twitter_client = None
                _drop_twitter_related_modules()


if __name__ == "__main__":
    unittest.main()
