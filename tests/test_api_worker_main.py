import asyncio
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
        spec = importlib.util.spec_from_file_location("api_worker_main_under_test", worker_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["api_worker_main_under_test"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


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


class _FakeRedisQueueWithRequeueLock:
    def __init__(self) -> None:
        self.data = {
            api_worker.PROCESSING_KEY: ["job:1", "job:2"],
            api_worker.API_QUEUE_KEY: [],
        }
        self.lock_values = {}
        self.rpush_calls = 0
        self.set_calls = []

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.lock_values:
            return False
        self.lock_values[key] = value
        return True

    async def lrange(self, key, start, end):
        values = list(self.data.get(key, []))
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    async def rpush(self, key, *values):
        self.rpush_calls += 1
        self.data.setdefault(key, []).extend(values)

    async def delete(self, key):
        self.data.pop(key, None)


class ApiWorkerStartupRequeueLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_requeue_lock_allows_only_single_worker(self) -> None:
        fake_redis = _FakeRedisQueueWithRequeueLock()
        stop_evt = asyncio.Event()
        stop_evt.set()
        requeue_lock_key = f"{api_worker.PROCESSING_KEY}:requeue_lock"

        with unittest.mock.patch.object(api_worker, "get_redis_queue", return_value=fake_redis), unittest.mock.patch.object(
            api_worker, "_sweeper_loop", unittest.mock.AsyncMock()
        ), unittest.mock.patch.object(api_worker, "_queue_depth_loop", unittest.mock.AsyncMock()), unittest.mock.patch.object(
            api_worker.logger, "info"
        ) as info_log:
            await asyncio.gather(
                api_worker._worker_loop(stop_evt),
                api_worker._worker_loop(stop_evt),
            )

        self.assertEqual(fake_redis.rpush_calls, 1)
        self.assertEqual(fake_redis.data[api_worker.API_QUEUE_KEY], ["job:1", "job:2"])
        self.assertTrue(
            any(
                call.args and "requeue-on-start skipped; lock held by another worker" in call.args[0]
                for call in info_log.call_args_list
            )
        )

        self.assertEqual(len(fake_redis.set_calls), 2)
        for call in fake_redis.set_calls:
            self.assertEqual(call["key"], requeue_lock_key)
            self.assertTrue(call["nx"])
            self.assertEqual(call["ex"], api_worker.REQUEUE_LOCK_TTL_SEC)


if __name__ == "__main__":
    unittest.main()
