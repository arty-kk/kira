import importlib
import os
import sys
import unittest


_TWITTER_ENV_VARS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "TWITTER_BEARER_TOKEN",
)


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


class TwitterClientLazyInitTests(unittest.IsolatedAsyncioTestCase):
    def test_import_does_not_fail_without_twitter_env(self) -> None:
        saved_env = {key: os.environ.get(key) for key in _TWITTER_ENV_VARS}
        try:
            for key in _TWITTER_ENV_VARS:
                os.environ.pop(key, None)

            _drop_twitter_related_modules()

            importlib.import_module("app.services.addons")
            importlib.import_module("app.tasks.periodic")
        finally:
            _drop_twitter_related_modules()
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def test_post_tweet_raises_config_error_when_creds_missing(self) -> None:
        module = importlib.import_module("app.clients.twitter_client")

        original_values = {key: getattr(module.settings, key, None) for key in _TWITTER_ENV_VARS}
        try:
            for key in _TWITTER_ENV_VARS:
                setattr(module.settings, key, None)
            module._twitter_client = None

            with self.assertRaisesRegex(RuntimeError, "Missing env vars") as ctx:
                await module.post_tweet("hello")

            for key in _TWITTER_ENV_VARS:
                self.assertIn(key, str(ctx.exception))
        finally:
            for key, value in original_values.items():
                setattr(module.settings, key, value)
            module._twitter_client = None


if __name__ == "__main__":
    unittest.main()
