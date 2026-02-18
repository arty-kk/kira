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
    fake_queue_recovery = types.ModuleType("app.core.queue_recovery")
    fake_services = types.ModuleType("app.services")
    fake_responder = types.ModuleType("app.services.responder")

    fake_config.settings = types.SimpleNamespace()
    fake_openai_client.get_openai = lambda: None
    fake_openai_client.transcribe_audio_with_retry = lambda **_kwargs: ""
    fake_openai_client.classify_openai_error = lambda _exc: "other"
    fake_clients.openai_client = fake_openai_client

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

    async def _fake_requeue_processing_on_start(*_args, **_kwargs):
        return types.SimpleNamespace(moved_count=0, lock_acquired=True)

    fake_queue_recovery.requeue_processing_on_start = _fake_requeue_processing_on_start

    patch_modules = {
        "app": fake_app,
        "app.config": fake_config,
        "app.clients": fake_clients,
        "app.clients.openai_client": fake_openai_client,
        "app.core": fake_core,
        "app.core.media_limits": fake_media_limits,
        "app.core.memory": fake_memory,
        "app.core.queue_recovery": fake_queue_recovery,
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

    async def test_worker_fails_fast_when_redis_queue_client_is_missing(self) -> None:
        close_mock = unittest.mock.AsyncMock()
        sweeper_mock = unittest.mock.AsyncMock()
        depth_mock = unittest.mock.AsyncMock()

        with unittest.mock.patch.object(api_worker, "get_redis_queue", return_value=None), unittest.mock.patch.object(
            api_worker, "_sweeper_loop", sweeper_mock
        ), unittest.mock.patch.object(api_worker, "_queue_depth_loop", depth_mock), unittest.mock.patch.object(
            api_worker, "close_redis_pools", close_mock
        ), unittest.mock.patch.object(api_worker.logger, "exception") as exception_log:
            with self.assertRaises(SystemExit) as ctx:
                await api_worker._async_main()

        self.assertEqual(ctx.exception.code, 1)
        close_mock.assert_awaited_once()
        exception_log.assert_called_once()
        self.assertEqual(
            exception_log.call_args.args[0],
            "api_worker: worker crashed unexpectedly",
        )
        self.assertIn("exc_info", exception_log.call_args.kwargs)
        sweeper_mock.assert_not_called()
        sweeper_mock.assert_not_awaited()
        depth_mock.assert_not_called()
        depth_mock.assert_not_awaited()


class _FakeRedisQueueWithRequeueLock:
    def __init__(self) -> None:
        self.data = {
            api_worker.PROCESSING_KEY: ["job:1", "job:2"],
            api_worker.API_QUEUE_KEY: [],
        }
        self.set_calls = []


class ApiWorkerStartupRequeueLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_requeue_lock_allows_only_single_worker(self) -> None:
        fake_redis = _FakeRedisQueueWithRequeueLock()
        stop_evt = asyncio.Event()
        stop_evt.set()

        with unittest.mock.patch.object(api_worker, "get_redis_queue", return_value=fake_redis), unittest.mock.patch.object(
            api_worker, "_sweeper_loop", unittest.mock.AsyncMock()
        ), unittest.mock.patch.object(api_worker, "_queue_depth_loop", unittest.mock.AsyncMock()), unittest.mock.patch.object(
            api_worker.logger, "info"
        ) as info_log, unittest.mock.patch.object(
            api_worker,
            "requeue_processing_on_start",
            new=unittest.mock.AsyncMock(
                side_effect=[
                    types.SimpleNamespace(moved_count=2, lock_acquired=True),
                    types.SimpleNamespace(moved_count=0, lock_acquired=False),
                ]
            ),
        ) as requeue_mock:
            await asyncio.gather(
                api_worker._worker_loop(stop_evt),
                api_worker._worker_loop(stop_evt),
            )

        self.assertEqual(requeue_mock.await_count, 2)

        self.assertTrue(
            any(
                call.args and "requeue-on-start skipped; lock held by another worker" in call.args[0]
                for call in info_log.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.args
                and call.args[0] == "api_worker: requeued %d pending from %s"
                and call.args[1:] == (2, api_worker.PROCESSING_KEY)
                for call in info_log.call_args_list
            )
        )


class _FailThenRecoverRedisQueue:
    def __init__(self, *, payload=None, error=None):
        self.payload = payload
        self.error = error or RuntimeError("redis down")
        self.calls = 0

    async def brpoplpush(self, *_args, **_kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.calls == 1:
            raise self.error
        return self.payload

    async def set(self, *_args, **_kwargs):
        return False


class _HealthyRedisQueue:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def brpoplpush(self, *_args, **_kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.calls == 1:
            return self.payload
        return None

    async def set(self, *_args, **_kwargs):
        return False


class ApiWorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_recovers_after_brpoplpush_failure_and_uses_new_client(self) -> None:
        stop_evt = asyncio.Event()
        payload = '{"request_id":"1"}'
        first_client = _FailThenRecoverRedisQueue()
        second_client = _HealthyRedisQueue(payload)

        get_redis_queue_mock = unittest.mock.Mock(side_effect=[first_client, second_client])
        close_mock = unittest.mock.AsyncMock()
        async def _fake_handle_job(raw, redis_client):
            stop_evt.set()

        with unittest.mock.patch.object(api_worker, "get_redis_queue", get_redis_queue_mock), unittest.mock.patch.object(
            api_worker, "close_redis_pools", close_mock
        ), unittest.mock.patch.object(api_worker, "_sweeper_loop", unittest.mock.AsyncMock()), unittest.mock.patch.object(
            api_worker, "_queue_depth_loop", unittest.mock.AsyncMock()
        ), unittest.mock.patch.object(api_worker, "_recovery_jitter", return_value=0.0), unittest.mock.patch.object(api_worker, "_handle_job", new=unittest.mock.AsyncMock(side_effect=_fake_handle_job)) as handle_job_mock:
            await api_worker._worker_loop(stop_evt)

        close_mock.assert_awaited_once()
        self.assertEqual(get_redis_queue_mock.call_count, 2)
        handle_job_mock.assert_awaited_once_with(payload, second_client)
        self.assertEqual(first_client.calls, 1)
        self.assertGreaterEqual(second_client.calls, 1)

    async def test_worker_retries_when_recovered_client_is_none(self) -> None:
        stop_evt = asyncio.Event()
        payload = '{"request_id":"2"}'
        first_client = _FailThenRecoverRedisQueue()
        second_client = _HealthyRedisQueue(payload)

        get_redis_queue_mock = unittest.mock.Mock(side_effect=[first_client, None, second_client])
        close_mock = unittest.mock.AsyncMock()

        async def _fake_handle_job(raw, redis_client):
            stop_evt.set()

        with unittest.mock.patch.object(api_worker, "get_redis_queue", get_redis_queue_mock), unittest.mock.patch.object(
            api_worker, "close_redis_pools", close_mock
        ), unittest.mock.patch.object(api_worker, "_sweeper_loop", unittest.mock.AsyncMock()), unittest.mock.patch.object(
            api_worker, "_queue_depth_loop", unittest.mock.AsyncMock()
        ), unittest.mock.patch.object(api_worker, "_recovery_jitter", return_value=0.0), unittest.mock.patch.object(api_worker, "_handle_job", new=unittest.mock.AsyncMock(side_effect=_fake_handle_job)) as handle_job_mock:
            await api_worker._worker_loop(stop_evt)

        self.assertEqual(get_redis_queue_mock.call_count, 3)
        self.assertEqual(close_mock.await_count, 2)
        handle_job_mock.assert_awaited_once_with(payload, second_client)
        self.assertEqual(first_client.calls, 1)
        self.assertGreaterEqual(second_client.calls, 1)


if __name__ == "__main__":
    unittest.main()
