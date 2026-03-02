import importlib
import os
import sys
import unittest
from contextlib import contextmanager
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


class TgPostManagerDownloadBytesTests(unittest.IsolatedAsyncioTestCase):
    def _import_module(self):
        _drop_module("app.services.addons.tg_post_manager")
        with _required_app_import_env():
            return importlib.import_module("app.services.addons.tg_post_manager")

    async def test_download_bytes_uses_shared_http_client(self) -> None:
        module = self._import_module()
        with patch.object(module.http_client, "get_bytes", AsyncMock(return_value=b"abc")) as get_mock:
            data = await module._download_bytes("https://example.com/a.png", timeout=7.5)

        self.assertEqual(data, b"abc")
        get_mock.assert_awaited_once_with("https://example.com/a.png", timeout_sec=7.5, retries=1)

    async def test_download_bytes_returns_none_on_http_error(self) -> None:
        module = self._import_module()
        with patch.object(module.http_client, "get_bytes", AsyncMock(side_effect=RuntimeError("boom"))):
            data = await module._download_bytes("https://example.com/a.png", timeout=7.5)

        self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
