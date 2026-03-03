import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
import unittest.mock

from contextlib import asynccontextmanager


def _load_api_worker():
    fake_app = types.ModuleType("app")
    fake_config = types.ModuleType("app.config")
    fake_clients = types.ModuleType("app.clients")
    fake_openai_client = types.ModuleType("app.clients.openai_client")
    fake_core = types.ModuleType("app.core")
    fake_media_limits = types.ModuleType("app.core.media_limits")
    fake_memory = types.ModuleType("app.core.memory")
    fake_queue_recovery = types.ModuleType("app.core.queue_recovery")
    fake_temp_files = types.ModuleType("app.core.temp_files")
    fake_services = types.ModuleType("app.services")
    fake_responder = types.ModuleType("app.services.responder")
    fake_dialog_logger = types.ModuleType("app.services.dialog_logger")

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

    async def _noop_async(*_args, **_kwargs):
        return None

    fake_dialog_logger.start_dialog_logger = _noop_async
    fake_dialog_logger.shutdown_dialog_logger = _noop_async

    async def _fake_requeue_processing_on_start(*_args, **_kwargs):
        return types.SimpleNamespace(moved_count=0, lock_acquired=True)

    @asynccontextmanager
    async def _fake_managed_temp_file(*, data: bytes | None = None, suffix: str = ""):
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            if data is not None:
                tmp.write(data)
            path = tmp.name
        try:
            yield path
        finally:
            pathlib.Path(path).unlink(missing_ok=True)

    async def _fake_open_binary_read(path: str):
        return open(path, "rb")

    fake_queue_recovery.requeue_processing_on_start = _fake_requeue_processing_on_start
    fake_temp_files.managed_temp_file = _fake_managed_temp_file
    fake_temp_files.open_binary_read = _fake_open_binary_read

    patch_modules = {
        "app": fake_app,
        "app.config": fake_config,
        "app.clients": fake_clients,
        "app.clients.openai_client": fake_openai_client,
        "app.core": fake_core,
        "app.core.media_limits": fake_media_limits,
        "app.core.memory": fake_memory,
        "app.core.queue_recovery": fake_queue_recovery,
        "app.core.temp_files": fake_temp_files,
        "app.services": fake_services,
        "app.services.responder": fake_responder,
        "app.services.dialog_logger": fake_dialog_logger,
    }
    previous = {name: sys.modules.get(name) for name in patch_modules}

    try:
        sys.modules.update(patch_modules)
        worker_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "tasks" / "api_worker.py"
        spec = importlib.util.spec_from_file_location("api_worker_inflight_under_test", worker_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["api_worker_inflight_under_test"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


api_worker = _load_api_worker()


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._commands = []

    def rpush(self, key, value):
        self._commands.append(("rpush", key, value))
        return self

    def expire(self, key, ttl):
        self._commands.append(("expire", key, ttl))
        return self

    async def execute(self):
        self._redis.pipeline_calls.append(self._commands)
        return [True] * len(self._commands)


class _FakeRedisQueue:
    def __init__(self, existing_value, *, scripted_get_values=None):
        self.values = {"api:job:req-123": existing_value}
        self.scripted_get_values = list(scripted_get_values or [])
        self.set_calls = []
        self.pipeline_calls = []
        self.lrem_calls = []

    async def set(self, key, value, ex=None, nx=None):
        self.set_calls.append({"key": key, "value": value, "ex": ex, "nx": nx})
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        if self.scripted_get_values:
            return self.scripted_get_values.pop(0)
        return self.values.get(key)

    async def eval(self, script, numkeys, key, observed_value, now_ts, stale_after, ttl):
        self.set_calls.append(
            {
                "key": key,
                "value": "<eval>",
                "ex": int(ttl),
                "nx": None,
                "observed_value": observed_value,
            }
        )
        assert numkeys == 1
        assert "inflight:" in script

        current_value = self.values.get(key)
        if current_value != observed_value:
            return 0
        if not isinstance(current_value, str) or not current_value.startswith("inflight:"):
            return 0

        try:
            inflight_ts = int(current_value.split(":", 1)[1])
        except (TypeError, ValueError):
            return 0

        now_ts = int(now_ts)
        stale_after = int(stale_after)
        ttl = int(ttl)
        if now_ts - inflight_ts <= stale_after:
            return 0

        self.values[key] = f"inflight:{now_ts}"
        return 1

    def pipeline(self):
        return _FakePipeline(self)

    async def lrem(self, key, count, value):
        self.lrem_calls.append((key, count, value))
        return 1


class ApiWorkerInflightTests(unittest.IsolatedAsyncioTestCase):
    def test_source_contains_expected_anchors(self) -> None:
        source = pathlib.Path(api_worker.__file__).read_text(encoding="utf-8")
        for anchor in (
            "_handle_job",
            "if not claimed",
            "duplicate_request",
            "inflight:",
        ):
            self.assertIn(anchor, source)

    async def test_inflight_newer_than_stale_window_skips_duplicate_publish(self) -> None:
        now = 10_000
        inflight_age = api_worker.RESPOND_TIMEOUT + 1
        self.assertLess(inflight_age, api_worker.INFLIGHT_STALE_AFTER_SEC)
        existing_ts = now - inflight_age

        redis_queue = _FakeRedisQueue(existing_value=f"inflight:{existing_ts}")
        job = {
            "request_id": "req-123",
            "result_key": "result:req-123",
            "chat_id": 1,
            "memory_uid": 2,
            "persona_owner_id": 3,
            "knowledge_owner_id": 7,
            "persona_profile_id": "profile",
            "msg_id": 4,
            "text": "hello",
        }

        with unittest.mock.patch.object(api_worker.time, "time", return_value=now):
            await api_worker._handle_job(json.dumps(job), redis_queue)

        self.assertEqual(len(redis_queue.set_calls), 2)
        self.assertTrue(redis_queue.set_calls[0]["nx"])
        self.assertEqual(redis_queue.pipeline_calls, [])
        self.assertEqual(len(redis_queue.lrem_calls), 1)


    async def test_handle_job_forwards_precomputed_rag_payload_to_responder(self) -> None:
        now = 30_000
        redis_queue = _FakeRedisQueue(existing_value="inflight:0")
        precomputed_rag_hits = [[0.88, "id1", "chunk"]]
        query_embedding = [0.11, 0.22]
        job = {
            "request_id": "req-777",
            "result_key": "result:req-777",
            "chat_id": 1,
            "memory_uid": 2,
            "persona_owner_id": 3,
            "knowledge_owner_id": 7,
            "persona_profile_id": "profile",
            "msg_id": 4,
            "text": "hello",
            "precomputed_rag_hits": precomputed_rag_hits,
            "query_embedding": query_embedding,
            "embedding_model": "text-embedding-3-large",
            "rag_precheck_source": "api_worker_tag_precheck",
        }

        with (
            unittest.mock.patch.object(api_worker.time, "time", return_value=now),
            unittest.mock.patch.object(
                api_worker,
                "respond_to_user",
                new=unittest.mock.AsyncMock(return_value="ok"),
            ) as responder_mock,
        ):
            await api_worker._handle_job(json.dumps(job), redis_queue)

        responder_mock.assert_awaited_once()
        kwargs = responder_mock.await_args.kwargs
        self.assertEqual(kwargs.get("precomputed_rag_hits"), precomputed_rag_hits)
        self.assertEqual(kwargs.get("query_embedding"), query_embedding)
        self.assertEqual(kwargs.get("embedding_model"), "text-embedding-3-large")
        self.assertEqual(kwargs.get("rag_precheck_source"), "api_worker_tag_precheck")

    async def test_handle_job_invalid_non_string_request_id_goes_to_dlq(self) -> None:
        redis_queue = _FakeRedisQueue(existing_value=None)
        raw = json.dumps(
            {
                "request_id": 123,
                "result_key": "result:req-123",
                "chat_id": 1,
                "memory_uid": 2,
                "persona_owner_id": 3,
                "knowledge_owner_id": 7,
                "persona_profile_id": "profile",
                "msg_id": 4,
                "text": "hello",
            }
        )

        with unittest.mock.patch.object(
            api_worker,
            "_push_dlq",
            new=unittest.mock.AsyncMock(),
        ) as push_dlq_mock:
            await api_worker._handle_job(raw, redis_queue)

        self.assertEqual(redis_queue.lrem_calls, [(api_worker.PROCESSING_KEY, 1, raw)])
        push_dlq_mock.assert_awaited_once_with(
            redis_queue,
            raw=raw,
            error_type="invalid_job",
            request_id=None,
            reason="missing_request_or_result_key",
            chat_id=1,
            persona_owner_id=3,
        )

    async def test_two_workers_race_on_stale_inflight_only_first_claims(self) -> None:
        now = 20_000
        stale_ts = now - api_worker.INFLIGHT_STALE_AFTER_SEC - 20
        stale_value = f"inflight:{stale_ts}"
        redis_queue = _FakeRedisQueue(
            existing_value=stale_value,
            scripted_get_values=[stale_value, stale_value],
        )
        job = {
            "request_id": "req-123",
            "result_key": "result:req-123",
            "chat_id": 1,
            "memory_uid": 2,
            "persona_owner_id": 3,
            "knowledge_owner_id": 7,
            "persona_profile_id": "profile",
            "msg_id": 4,
            "text": "hello",
        }
        release_first = api_worker.asyncio.Event()
        first_started = api_worker.asyncio.Event()

        async def _fake_respond_to_user(**_kwargs):
            first_started.set()
            await release_first.wait()
            return "ok"

        with (
            unittest.mock.patch.object(api_worker.time, "time", return_value=now),
            unittest.mock.patch.object(
                api_worker,
                "respond_to_user",
                new=unittest.mock.AsyncMock(side_effect=_fake_respond_to_user),
            ) as responder_mock,
        ):
            first_task = api_worker.asyncio.create_task(api_worker._handle_job(json.dumps(job), redis_queue))
            await first_started.wait()
            await api_worker._handle_job(json.dumps(job), redis_queue)
            second_seen_calls = responder_mock.await_count
            release_first.set()
            await first_task

        self.assertGreaterEqual(second_seen_calls, 1)
        self.assertEqual(second_seen_calls, responder_mock.await_count)

        self.assertEqual(len(redis_queue.pipeline_calls), 1)
        payload = json.loads(redis_queue.pipeline_calls[0][0][2])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reply"], "ok")


if __name__ == "__main__":
    unittest.main()
