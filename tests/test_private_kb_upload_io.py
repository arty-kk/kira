import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager

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


class PrivateKbUploadIoTests(unittest.IsolatedAsyncioTestCase):
    def _import_module(self):
        _drop_module("app.bot.handlers.private")
        _drop_module("app.bot.handlers")
        with _required_app_import_env():
            return importlib.import_module("app.bot.handlers.private")

    async def test_read_utf8_json_text_limited_rejects_oversized(self) -> None:
        module = self._import_module()
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(b"{}")
            path = tmp.name
        try:
            with self.assertRaises(ValueError):
                await module._read_utf8_json_text_limited(path, max_bytes=1)
        finally:
            os.remove(path)

    async def test_read_utf8_json_text_limited_reads_valid_utf8_sig(self) -> None:
        module = self._import_module()
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(b"\xef\xbb\xbf{\"ok\":1}")
            path = tmp.name
        try:
            text = await module._read_utf8_json_text_limited(path, max_bytes=1024)
            self.assertEqual(text, '{"ok":1}')
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
