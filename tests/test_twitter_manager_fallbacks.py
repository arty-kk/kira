import asyncio
import importlib
import os
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class TwitterManagerFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _import_module(self):
        _drop_module("app.services.addons.twitter_manager")
        with _required_app_import_env():
            return importlib.import_module("app.services.addons.twitter_manager")

    @staticmethod
    def _fake_persona() -> SimpleNamespace:
        event = asyncio.Event()
        event.set()
        return SimpleNamespace(
            _restored_evt=event,
            _mods_cache={},
            style_modifiers=AsyncMock(return_value={}),
            style_guidelines=AsyncMock(return_value="guidelines"),
            process_interaction=AsyncMock(return_value=None),
        )

    async def test_generate_and_post_tweet_uses_normalized_fallback(self) -> None:
        module = self._import_module()
        fake_persona = self._fake_persona()

        original_fallbacks = module.settings.TWITTER_FALLBACK_TWEETS
        try:
            with (
                patch.object(module, "get_persona", AsyncMock(return_value=fake_persona)),
                patch.object(module, "load_context", AsyncMock(return_value=[])),
                patch.object(module, "build_system_prompt", AsyncMock(return_value="system")),
                patch.object(module, "_call_openai_with_retry", AsyncMock(side_effect=[object(), object()])),
                patch.object(module, "_get_output_text", side_effect=["news", ""]),
                patch.object(module, "post_tweet", AsyncMock()) as post_tweet_mock,
                patch.object(module, "push_message", AsyncMock(return_value=None)),
                patch.object(module.random, "choice", side_effect=lambda seq: seq[0]),
            ):
                module.settings.TWITTER_FALLBACK_TWEETS = [None, "   ", "  fallback tweet  ", 123]
                await module.generate_and_post_tweet()

            post_tweet_mock.assert_awaited_once_with("fallback tweet")
        finally:
            module.settings.TWITTER_FALLBACK_TWEETS = original_fallbacks

    async def test_generate_and_post_tweet_uses_safe_default_when_fallbacks_invalid(self) -> None:
        module = self._import_module()
        fake_persona = self._fake_persona()

        original_fallbacks = module.settings.TWITTER_FALLBACK_TWEETS
        try:
            with (
                patch.object(module, "get_persona", AsyncMock(return_value=fake_persona)),
                patch.object(module, "load_context", AsyncMock(return_value=[])),
                patch.object(module, "build_system_prompt", AsyncMock(return_value="system")),
                patch.object(module, "_call_openai_with_retry", AsyncMock(side_effect=[object(), object()])),
                patch.object(module, "_get_output_text", side_effect=["news", ""]),
                patch.object(module, "post_tweet", AsyncMock()) as post_tweet_mock,
                patch.object(module, "push_message", AsyncMock(return_value=None)),
                patch.object(module.random, "choice", side_effect=lambda seq: seq[0]),
            ):
                module.settings.TWITTER_FALLBACK_TWEETS = [None, "   ", 123]
                await module.generate_and_post_tweet()

            post_tweet_mock.assert_awaited_once_with(module.SAFE_DEFAULT_TWEET)
        finally:
            module.settings.TWITTER_FALLBACK_TWEETS = original_fallbacks


if __name__ == "__main__":
    unittest.main()
