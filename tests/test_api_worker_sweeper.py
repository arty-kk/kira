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
        spec = importlib.util.spec_from_file_location("api_worker_sweeper_under_test", worker_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["api_worker_sweeper_under_test"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


api_worker = _load_api_worker()


class _FakeRedisSweeper:
    def __init__(self, processing_items, job_values, *, fail_non_atomic_push=False, eval_unavailable=False):
        self.data = {
            api_worker.PROCESSING_KEY: list(processing_items),
            api_worker.API_QUEUE_KEY: [],
        }
        self.job_values = dict(job_values)
        self.fail_non_atomic_push = fail_non_atomic_push
        self.eval_calls = []
        self.eval_unavailable = eval_unavailable

    async def llen(self, key):
        return len(self.data.get(key, []))

    async def lrange(self, key, start, end):
        values = list(self.data.get(key, []))
        if not values:
            return []

        norm_start = max(start, 0)
        norm_end = len(values) - 1 if end == -1 else min(end, len(values) - 1)
        if norm_start > norm_end:
            return []
        return values[norm_start : norm_end + 1]

    async def get(self, key):
        return self.job_values.get(key)

    async def lrem(self, key, count, value):
        if count != 1:
            raise AssertionError("Test fake supports only count=1")
        items = self.data.get(key, [])
        for idx, raw in enumerate(items):
            if raw == value:
                del items[idx]
                return 1
        return 0

    async def lpush(self, key, value):
        if self.fail_non_atomic_push:
            raise RuntimeError("simulated crash after lrem before push")
        self.data.setdefault(key, []).insert(0, value)
        return len(self.data[key])

    async def eval(self, script, numkeys, *args):
        self.eval_calls.append((script, numkeys, args))
        if self.eval_unavailable:
            raise RuntimeError("unknown command 'eval'")
        if numkeys != 2:
            raise AssertionError("Unsupported eval numkeys")
        processing_key, queue_key, raw, push_side = args
        removed = await self.lrem(processing_key, 1, raw)
        if removed <= 0:
            return 0
        if push_side == "left":
            self.data.setdefault(queue_key, []).insert(0, raw)
        elif push_side == "right":
            self.data.setdefault(queue_key, []).append(raw)
        else:
            raise AssertionError(f"Unsupported push_side={push_side}")
        return 1

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    async def watch(self, _key):
        return None

    async def lrange(self, key, start, end):
        return await self.redis.lrange(key, start, end)

    def multi(self):
        self.ops = []

    def lrem(self, key, count, value):
        self.ops.append(("lrem", key, count, value))

    def lpush(self, key, value):
        self.ops.append(("lpush", key, value))

    def rpush(self, key, value):
        self.ops.append(("rpush", key, value))

    async def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "lrem":
                out.append(await self.redis.lrem(op[1], op[2], op[3]))
            elif op[0] == "lpush":
                out.append(await self.redis.lpush(op[1], op[2]))
            elif op[0] == "rpush":
                self.redis.data.setdefault(op[1], []).append(op[2])
                out.append(len(self.redis.data[op[1]]))
        return out

    async def reset(self):
        return None


class ApiWorkerSweeperBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_processing_item_requeued_with_batch_sweep(self):
        stale_raw = '{"request_id":"stale-tail"}'
        fake_redis = _FakeRedisSweeper(
            processing_items=[stale_raw],
            job_values={api_worker.JOB_KEY_PREFIX + "stale-tail": "inflight:0"},
        )
        stop_evt = asyncio.Event()
        sleep_calls = 0

        async def _sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 1:
                stop_evt.set()

        with unittest.mock.patch.object(api_worker, "API_PROCESSING_SWEEP_BATCH", 1), unittest.mock.patch.object(
            api_worker, "INFLIGHT_STALE_AFTER_SEC", 10
        ), unittest.mock.patch.object(api_worker.time, "time", return_value=1000), unittest.mock.patch.object(
            api_worker.asyncio, "sleep", new=_sleep
        ):
            await api_worker._sweeper_loop(stop_evt, fake_redis)

        self.assertEqual(fake_redis.data[api_worker.PROCESSING_KEY], [])
        self.assertEqual(fake_redis.data[api_worker.API_QUEUE_KEY], [stale_raw])

    async def test_round_robin_windows_eventually_pick_stale_item(self):
        stale_raw = '{"request_id":"stale-head"}'
        tail_1 = '{"request_id":"tail-1"}'
        tail_2 = '{"request_id":"tail-2"}'
        tail_3 = '{"request_id":"tail-3"}'
        tail_4 = '{"request_id":"tail-4"}'

        fake_redis = _FakeRedisSweeper(
            processing_items=[stale_raw, tail_1, tail_2, tail_3, tail_4],
            job_values={
                api_worker.JOB_KEY_PREFIX + "stale-head": "inflight:0",
                api_worker.JOB_KEY_PREFIX + "tail-1": "inflight:995",
                api_worker.JOB_KEY_PREFIX + "tail-2": "inflight:995",
                api_worker.JOB_KEY_PREFIX + "tail-3": "inflight:995",
                api_worker.JOB_KEY_PREFIX + "tail-4": "inflight:995",
            },
        )

        stop_evt = asyncio.Event()
        sleep_calls = 0

        async def _sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                stop_evt.set()

        with unittest.mock.patch.object(api_worker, "API_PROCESSING_SWEEP_BATCH", 2), unittest.mock.patch.object(
            api_worker, "INFLIGHT_STALE_AFTER_SEC", 10
        ), unittest.mock.patch.object(api_worker.time, "time", return_value=1000), unittest.mock.patch.object(
            api_worker.asyncio, "sleep", new=_sleep
        ):
            await api_worker._sweeper_loop(stop_evt, fake_redis)

        self.assertNotIn(stale_raw, fake_redis.data[api_worker.PROCESSING_KEY])
        self.assertEqual(fake_redis.data[api_worker.API_QUEUE_KEY], [stale_raw])


    async def test_stale_job_reclaimed_when_eval_unavailable_via_watch_multi(self):
        stale_raw = '{"request_id":"stale-fallback"}'
        fake_redis = _FakeRedisSweeper(
            processing_items=[stale_raw],
            job_values={api_worker.JOB_KEY_PREFIX + "stale-fallback": "inflight:0"},
            eval_unavailable=True,
        )
        stop_evt = asyncio.Event()

        async def _sleep(_seconds):
            stop_evt.set()

        with unittest.mock.patch.object(api_worker, "API_PROCESSING_SWEEP_BATCH", 1), unittest.mock.patch.object(
            api_worker, "INFLIGHT_STALE_AFTER_SEC", 10
        ), unittest.mock.patch.object(api_worker.time, "time", return_value=1000), unittest.mock.patch.object(
            api_worker.asyncio, "sleep", new=_sleep
        ):
            await api_worker._sweeper_loop(stop_evt, fake_redis)

        self.assertEqual(fake_redis.data[api_worker.PROCESSING_KEY], [])
        self.assertEqual(fake_redis.data[api_worker.API_QUEUE_KEY], [stale_raw])

    async def test_stale_job_not_lost_when_reclaim_push_would_crash_non_atomic(self):
        stale_raw = '{"request_id":"stale-crash"}'
        fake_redis = _FakeRedisSweeper(
            processing_items=[stale_raw],
            job_values={api_worker.JOB_KEY_PREFIX + "stale-crash": "inflight:0"},
            fail_non_atomic_push=True,
        )
        stop_evt = asyncio.Event()

        async def _sleep(_seconds):
            stop_evt.set()

        with unittest.mock.patch.object(api_worker, "API_PROCESSING_SWEEP_BATCH", 1), unittest.mock.patch.object(
            api_worker, "INFLIGHT_STALE_AFTER_SEC", 10
        ), unittest.mock.patch.object(api_worker.time, "time", return_value=1000), unittest.mock.patch.object(
            api_worker.asyncio, "sleep", new=_sleep
        ):
            await api_worker._sweeper_loop(stop_evt, fake_redis)

        in_processing = stale_raw in fake_redis.data[api_worker.PROCESSING_KEY]
        in_queue = stale_raw in fake_redis.data[api_worker.API_QUEUE_KEY]
        self.assertTrue(in_processing or in_queue)
        self.assertFalse(in_processing and in_queue)
        self.assertGreaterEqual(len(fake_redis.eval_calls), 1)


if __name__ == "__main__":
    unittest.main()
