import importlib.util
import pathlib
import sys
import types
import unittest
import unittest.mock


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

    fake_media_limits.ALLOWED_IMAGE_MIMES = {"image/png"}
    fake_media_limits.ALLOWED_VOICE_MIMES = {"audio/ogg"}
    fake_media_limits.API_MAX_IMAGE_BYTES = 5 * 1024 * 1024
    fake_media_limits.API_MAX_VOICE_BYTES = 25 * 1024 * 1024
    fake_media_limits.clean_base64_payload = lambda value: value
    fake_media_limits.decode_base64_payload = (
        lambda value: value if isinstance(value, bytes) else b""
    )

    fake_memory.get_redis_queue = lambda: None
    fake_memory.close_redis_pools = lambda: None
    fake_responder.respond_to_user = lambda **kwargs: None

    sys.modules["app"] = fake_app
    sys.modules["app.config"] = fake_config
    sys.modules["app.clients"] = fake_clients
    sys.modules["app.clients.openai_client"] = fake_openai_client
    sys.modules["app.core"] = fake_core
    sys.modules["app.core.media_limits"] = fake_media_limits
    sys.modules["app.core.memory"] = fake_memory
    sys.modules["app.services"] = fake_services
    sys.modules["app.services.responder"] = fake_responder

    worker_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "api_worker.py"
    spec = importlib.util.spec_from_file_location("api_worker_main_under_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["api_worker_main_under_test"] = module
    spec.loader.exec_module(module)
    return module


api_worker = _load_api_worker()


class ApiWorkerMainFailFastTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_crash_before_stop_signal_fails_fast(self) -> None:
        async def _failing_worker(_stop_evt):
            raise RuntimeError("worker boom")

        close_mock = unittest.mock.AsyncMock()

        with unittest.mock.patch.object(api_worker, "_worker_loop", _failing_worker), unittest.mock.patch.object(
            api_worker, "close_redis_pools", close_mock
        ), unittest.mock.patch.object(api_worker.logger, "exception") as exception_log:
            with self.assertRaises(SystemExit) as ctx:
                await api_worker._async_main()

        self.assertEqual(ctx.exception.code, 1)
        close_mock.assert_awaited_once()
        exception_log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
