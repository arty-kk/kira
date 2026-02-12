import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


def _load_api_worker():
    fake_app = types.ModuleType("app")
    fake_config = types.ModuleType("app.config")
    fake_clients = types.ModuleType("app.clients")
    fake_openai_client = types.ModuleType("app.clients.openai_client")
    fake_core = types.ModuleType("app.core")
    fake_media_limits = types.ModuleType("app.core.media_limits")
    fake_memory = types.ModuleType("app.core.memory")
    fake_services = types.ModuleType("app.services")
    fake_responder = types.ModuleType("app.services.responder")

    fake_config.settings = types.SimpleNamespace()

    fake_openai_client.get_openai = lambda: None
    fake_openai_client.transcribe_audio_with_retry = lambda **_kwargs: ""
    fake_openai_client.classify_openai_error = lambda _exc: "other"
    fake_clients.openai_client = fake_openai_client

    fake_media_limits.ALLOWED_IMAGE_MIMES = {"image/png"}
    fake_media_limits.ALLOWED_VOICE_MIMES = {
        "audio/ogg",
        "audio/opus",
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/mp4",
        "audio/m4a",
        "audio/aac",
    }
    fake_media_limits.API_MAX_IMAGE_BYTES = 5 * 1024 * 1024
    fake_media_limits.API_MAX_VOICE_BYTES = 25 * 1024 * 1024
    fake_media_limits.clean_base64_payload = lambda value: value
    fake_media_limits.decode_base64_payload = lambda value: value if isinstance(value, bytes) else b""

    fake_memory.get_redis_queue = lambda: None
    fake_memory.close_redis_pools = lambda: None

    fake_responder.respond_to_user = lambda **kwargs: None

    patch_modules = {
        "app": fake_app,
        "app.config": fake_config,
        "app.clients": fake_clients,
        "app.clients.openai_client": fake_openai_client,
        "app.core": fake_core,
        "app.core.media_limits": fake_media_limits,
        "app.core.memory": fake_memory,
        "app.services": fake_services,
        "app.services.responder": fake_responder,
    }
    previous = {name: sys.modules.get(name) for name in patch_modules}

    try:
        sys.modules.update(patch_modules)
        worker_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "api_worker.py"
        spec = importlib.util.spec_from_file_location("api_worker_under_test", worker_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["api_worker_under_test"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


api_worker = _load_api_worker()


class VoiceMimeDetectionTests(unittest.TestCase):
    def test_detect_wav(self) -> None:
        data = b"RIFF\x00\x00\x00\x00WAVEfmt "
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/wav")

    def test_detect_mp3_id3(self) -> None:
        data = b"ID3\x03\x00\x00\x00\x00\x00\x21"
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/mpeg")

    def test_detect_mp3_frame_sync(self) -> None:
        data = b"\xFF\xFB\x90\x64"
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/mpeg")

    def test_detect_mp4_m4a_brand(self) -> None:
        data = b"\x00\x00\x00\x18ftypM4A "
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/m4a")

    def test_detect_mp4_isom_brand(self) -> None:
        data = b"\x00\x00\x00\x18ftypisom"
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/mp4")

    def test_detect_ogg(self) -> None:
        data = b"OggS\x00\x02"
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/ogg")

    def test_detect_webm(self) -> None:
        data = b"\x1A\x45\xDF\xA3\x93\x42"
        self.assertEqual(api_worker.detect_voice_mime(data), "audio/webm")

    def test_detect_unknown(self) -> None:
        data = b"\x00\x01\x02\x03"
        self.assertIsNone(api_worker.detect_voice_mime(data))


class ErrorClassificationTests(unittest.TestCase):
    def test_voice_validation_error_codes(self) -> None:
        self.assertEqual(
            api_worker._classify_error({"code": "invalid_voice_format"}),
            "validation",
        )
        self.assertEqual(
            api_worker._classify_error({"code": "invalid_voice_mime"}),
            "validation",
        )
        self.assertEqual(
            api_worker._classify_error({"code": "voice_transcription_failed"}),
            "validation",
        )


if __name__ == "__main__":
    unittest.main()


class VoiceTranscriptionRetryWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_voice_bytes_returns_empty_on_final_failure(self) -> None:
        async def _boom(**_kwargs):
            raise RuntimeError("upstream down")

        with patch.object(api_worker.openai_client, "transcribe_audio_with_retry", side_effect=_boom):
            text = await api_worker._transcribe_voice_bytes(b"OggS\x00\x02", "audio/ogg")
        self.assertEqual(text, "")

    async def test_transcribe_voice_bytes_uses_configured_model(self) -> None:
        captured = {}

        async def _ok(**kwargs):
            captured.update(kwargs)
            return " hello "

        with patch.object(api_worker.openai_client, "transcribe_audio_with_retry", side_effect=_ok):
            text = await api_worker._transcribe_voice_bytes(b"OggS\x00\x02", "audio/ogg")
        self.assertEqual(text, "hello")
        self.assertEqual(captured.get("model"), api_worker.VOICE_TRANSCRIPTION_MODEL)

