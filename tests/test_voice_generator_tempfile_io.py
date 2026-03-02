import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

_REQUIRED_IMPORT_ENV = {
    "OPENAI_API_KEY": "test-openai-key",
    "DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "REDIS_URL_QUEUE": "redis://localhost:6379/1",
    "REDIS_URL_VECTOR": "redis://localhost:6379/2",
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "CELERY_BROKER_URL": "redis://localhost:6379/3",
    "ELEVENLABS_API_KEY": "test-elevenlabs-key",
}


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


class VoiceGeneratorTempFileIOTests(unittest.IsolatedAsyncioTestCase):
    def _import_module(self):
        sys.modules.pop("app.services.addons.voice_generator", None)
        with _required_app_import_env():
            return importlib.import_module("app.services.addons.voice_generator")

    async def _call_generate(self, module):
        return await module.generate_voice_for_reply(
            reply_text="hello world",
            user_id=1,
            chat_id=1,
            force=True,
        )

    def _base_patches(self, module, audio_bytes: bytes):
        fake_client = types.SimpleNamespace(synthesize=AsyncMock(return_value=audio_bytes))
        return (
            patch.object(module, "TTS_ENABLED", True),
            patch.object(module, "is_tts_eligible_short", return_value=True),
            patch.object(module, "get_user_lang", AsyncMock(return_value="en")),
            patch.object(module, "choose_voice", AsyncMock(return_value=(fake_client, "voice-id"))),
        )

    async def test_reencode_success_keeps_output_and_cleans_input(self):
        module = self._import_module()
        src = b"not-ogg-bytes"
        state = {"input_path": None}

        class _Proc:
            returncode = 0

            async def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "out.ogg")

            async def _fake_create_temp_path(*, suffix: str):
                self.assertEqual(suffix, ".ogg")
                return out_path

            async def _fake_subprocess(*args, **kwargs):
                input_index = args.index("-i") + 1
                state["input_path"] = args[input_index]
                with open(out_path, "wb") as f:
                    f.write(b"OggS" + b"x" * 80)
                return _Proc()

            with ExitStack() as stack:
                for ctx in self._base_patches(module, src):
                    stack.enter_context(ctx)
                stack.enter_context(patch.object(module, "create_temp_path", _fake_create_temp_path))
                stack.enter_context(patch.object(module.asyncio, "create_subprocess_exec", side_effect=_fake_subprocess))
                result_path = await self._call_generate(module)

            self.assertEqual(result_path, out_path)
            self.assertTrue(os.path.exists(result_path))
            self.assertIsNotNone(state["input_path"])
            self.assertFalse(os.path.exists(state["input_path"]))

    async def test_reencode_timeout_returns_none_and_cleans_output(self):
        module = self._import_module()

        class _Proc:
            returncode = 0

            async def wait(self):
                raise asyncio.TimeoutError

            def kill(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "timeout.ogg")

            async def _fake_create_temp_path(*, suffix: str):
                self.assertEqual(suffix, ".ogg")
                return out_path

            async def _fake_subprocess(*_args, **_kwargs):
                with open(out_path, "wb") as f:
                    f.write(b"partial")
                return _Proc()

            with ExitStack() as stack:
                for ctx in self._base_patches(module, b"not-ogg-bytes"):
                    stack.enter_context(ctx)
                stack.enter_context(patch.object(module, "create_temp_path", _fake_create_temp_path))
                stack.enter_context(patch.object(module.asyncio, "create_subprocess_exec", side_effect=_fake_subprocess))
                result_path = await self._call_generate(module)

            self.assertIsNone(result_path)
            self.assertFalse(os.path.exists(out_path))

    async def test_reencode_cancel_cleans_output(self):
        module = self._import_module()

        class _Proc:
            returncode = 0

            async def wait(self):
                raise asyncio.CancelledError

            def kill(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "cancel.ogg")

            async def _fake_create_temp_path(*, suffix: str):
                self.assertEqual(suffix, ".ogg")
                return out_path

            async def _fake_subprocess(*_args, **_kwargs):
                with open(out_path, "wb") as f:
                    f.write(b"partial")
                return _Proc()

            with ExitStack() as stack:
                for ctx in self._base_patches(module, b"not-ogg-bytes"):
                    stack.enter_context(ctx)
                stack.enter_context(patch.object(module, "create_temp_path", _fake_create_temp_path))
                stack.enter_context(patch.object(module.asyncio, "create_subprocess_exec", side_effect=_fake_subprocess))
                with self.assertRaises(asyncio.CancelledError):
                    await self._call_generate(module)

            self.assertFalse(os.path.exists(out_path))


if __name__ == "__main__":
    unittest.main()
