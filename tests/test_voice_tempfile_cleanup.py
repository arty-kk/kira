import importlib
import os
import sys
import types
import unittest
from contextlib import asynccontextmanager, contextmanager
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


class VoiceTempCleanupTests(unittest.IsolatedAsyncioTestCase):
    def _import_queue_worker(self):
        _drop_module("app.tasks.queue_worker")
        with _required_app_import_env():
            return importlib.import_module("app.tasks.queue_worker")

    def _import_api_worker(self):
        _drop_module("app.tasks.api_worker")
        with _required_app_import_env():
            return importlib.import_module("app.tasks.api_worker")

    async def test_queue_worker_transcribe_exits_temp_context_on_failure(self) -> None:
        module = self._import_queue_worker()
        state = {"entered": 0, "exited": 0}

        @asynccontextmanager
        async def _fake_managed_temp_file(**_kwargs):
            state["entered"] += 1
            try:
                yield __file__
            finally:
                state["exited"] += 1

        async def _get_file(_file_id):
            return object()

        async def _download(_file, _path):
            return None

        async def _boom(**_kwargs):
            raise RuntimeError("fail")

        with (
            patch.object(module, "managed_temp_file", _fake_managed_temp_file),
            patch.object(module, "open_binary_read", AsyncMock(return_value=open(__file__, "rb"))),
            patch.object(module, "BOT", types.SimpleNamespace(get_file=_get_file, download=_download)),
            patch.object(module.openai_client, "transcribe_audio_with_retry", side_effect=_boom),
        ):
            text = await module._transcribe_voice_file_id("f")

        self.assertEqual(text, "")
        self.assertEqual(state["entered"], 1)
        self.assertEqual(state["exited"], 1)

    async def test_api_worker_transcribe_exits_temp_context_on_failure(self) -> None:
        module = self._import_api_worker()
        state = {"entered": 0, "exited": 0}

        @asynccontextmanager
        async def _fake_managed_temp_file(**_kwargs):
            state["entered"] += 1
            try:
                yield __file__
            finally:
                state["exited"] += 1

        async def _boom(**_kwargs):
            raise RuntimeError("fail")

        with (
            patch.object(module, "managed_temp_file", _fake_managed_temp_file),
            patch.object(module, "open_binary_read", AsyncMock(return_value=open(__file__, "rb"))),
            patch.object(module.openai_client, "transcribe_audio_with_retry", side_effect=_boom),
        ):
            text = await module._transcribe_voice_bytes(b"OggS\x00\x02", "audio/ogg")

        self.assertEqual(text, "")
        self.assertEqual(state["entered"], 1)
        self.assertEqual(state["exited"], 1)


if __name__ == "__main__":
    unittest.main()
