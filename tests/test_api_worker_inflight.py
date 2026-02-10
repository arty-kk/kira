import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location("api_worker_inflight_under_test", worker_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["api_worker_inflight_under_test"] = module
    spec.loader.exec_module(module)
    return module


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
    def __init__(self, existing_value):
        self.existing_value = existing_value
        self.set_calls = []
        self.pipeline_calls = []
        self.lrem_calls = []

    async def set(self, key, value, ex=None, nx=None):
        self.set_calls.append({"key": key, "value": value, "ex": ex, "nx": nx})
        if nx:
            return False
        return True

    async def get(self, _key):
        return self.existing_value

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
            "Request is already in progress",
            "duplicate_request",
            "inflight:",
        ):
            self.assertIn(anchor, source)

    async def test_inflight_newer_than_stale_window_returns_duplicate(self) -> None:
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
            "persona_profile_id": "profile",
            "msg_id": 4,
            "text": "hello",
        }

        with unittest.mock.patch.object(api_worker.time, "time", return_value=now):
            await api_worker._handle_job(json.dumps(job), redis_queue)

        self.assertEqual(len(redis_queue.set_calls), 1)
        self.assertTrue(redis_queue.set_calls[0]["nx"])

        self.assertEqual(len(redis_queue.pipeline_calls), 1)
        pipeline_cmds = redis_queue.pipeline_calls[0]
        self.assertEqual(pipeline_cmds[0][0], "rpush")
        payload = json.loads(pipeline_cmds[0][2])
        self.assertEqual(payload["error"]["code"], "duplicate_request")
        self.assertEqual(payload["error"]["message"], "Request is already in progress")
        self.assertEqual(payload["error"]["status"], 409)

        self.assertEqual(len(redis_queue.lrem_calls), 1)


if __name__ == "__main__":
    unittest.main()
