import unittest
import unittest.mock
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import conversation
from app.tasks import api_worker


class _DummyResult:
    def __init__(self, *, scalar_value=None, rowcount=1):
        self._scalar_value = scalar_value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar_value


class _DummyDB:
    def __init__(self, *, with_persona: bool):
        self._with_persona = with_persona
        self._execute_calls = 0

    async def execute(self, _stmt):
        self._execute_calls += 1
        if self._execute_calls == 1:
            return _DummyResult(scalar_value="free")
        if self._with_persona and self._execute_calls == 2:
            return _DummyResult(rowcount=1)
        return _DummyResult()




class _WorkerPipeline:
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
        for op, key, value in self._commands:
            if op == "rpush":
                self._redis.storage.setdefault(key, []).append(value)
            elif op == "expire":
                self._redis.expire_calls.append((key, value))
        return [True] * len(self._commands)


class _WorkerRedis:
    def __init__(self):
        self.storage = {}
        self.expire_calls = []

    async def set(self, key, value, ex=None, nx=None):
        if nx and key in self.storage:
            return False
        self.storage[key] = value
        return True

    async def get(self, key):
        return self.storage.get(key)

    async def delete(self, key):
        self.storage.pop(key, None)
        return 1

    async def lrem(self, key, count, value):
        return 1

    def pipeline(self):
        return _WorkerPipeline(self)


class ConversationMemoryUidTests(unittest.IsolatedAsyncioTestCase):
    async def _run_endpoint(self, payload: conversation.ConversationRequest):
        captured_jobs = []

        async def _fake_send_job_and_wait(*, request_id, job):
            captured_jobs.append((request_id, job))
            return {
                "ok": True,
                "reply": "ok",
                "latency_ms": 1,
                "latency_breakdown": {
                    "queue_latency_ms": 0,
                    "worker_latency_ms": 1,
                },
            }

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _DummyDB(with_persona=payload.persona is not None)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/conversation",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        request = Request(scope)

        with (
            unittest.mock.patch.object(conversation, "_check_rate_limit", new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(conversation, "session_scope", _fake_session_scope),
            unittest.mock.patch.object(conversation, "_send_job_and_wait", _fake_send_job_and_wait),
            unittest.mock.patch.object(conversation, "register_api_memory_uid", new=unittest.mock.AsyncMock()) as register_mock,
            unittest.mock.patch.object(conversation, "inc_stats", new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(conversation, "update_cached_personas_for_owner", new=unittest.mock.AsyncMock()),
            unittest.mock.patch.object(conversation, "get_redis", return_value=None),
        ):
            await conversation.conversation_endpoint(
                payload,
                request,
                api_key={"user_id": 1, "id": 2},
                idempotency_key=None,
            )

        (_, job), = captured_jobs
        register_memory_uid = None
        if register_mock.await_args is not None:
            register_memory_uid = register_mock.await_args.args[1]
        return job, register_memory_uid


    async def _run_worker_for_job(self, job: dict):
        redis_queue = _WorkerRedis()
        respond_mock = unittest.mock.AsyncMock(return_value="ok")
        with (
            unittest.mock.patch.object(api_worker, "respond_to_user", new=respond_mock),
            unittest.mock.patch.object(api_worker, "_heartbeat_job", new=unittest.mock.AsyncMock(return_value=None)),
        ):
            await api_worker._handle_job(json.dumps(job), redis_queue)

        return respond_mock.await_args.kwargs

    async def test_job_memory_uid_scoped_for_persona(self):
        payload = conversation.ConversationRequest(
            user_id="user-1",
            message="hi",
            persona=conversation.PersonaConfig(name="Ava"),
        )

        job, register_memory_uid = await self._run_endpoint(payload)

        self.assertIsNotNone(job["persona_profile_id"])
        self.assertEqual(job["memory_uid"], register_memory_uid)
        self.assertNotEqual(job["memory_uid"], job["chat_id"])

    async def test_job_memory_uid_falls_back_without_persona(self):
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        job, register_memory_uid = await self._run_endpoint(payload)

        self.assertIsNone(job["persona_profile_id"])
        self.assertEqual(job["memory_uid"], job["chat_id"])
        self.assertEqual(job["memory_uid"], register_memory_uid)



    async def test_worker_uses_same_memory_uid_from_job_for_persona_profile(self):
        payload = conversation.ConversationRequest(
            user_id="user-1",
            message="hi",
            persona=conversation.PersonaConfig(name="Ava"),
        )

        job, register_memory_uid = await self._run_endpoint(payload)
        responder_kwargs = await self._run_worker_for_job(job)

        self.assertEqual(job["memory_uid"], register_memory_uid)
        self.assertEqual(responder_kwargs["memory_uid"], job["memory_uid"])
        self.assertEqual(responder_kwargs["user_id"], job["memory_uid"])


    async def test_worker_uses_same_memory_uid_from_job_without_persona_profile(self):
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        job, register_memory_uid = await self._run_endpoint(payload)
        responder_kwargs = await self._run_worker_for_job(job)

        self.assertEqual(job["memory_uid"], register_memory_uid)
        self.assertEqual(responder_kwargs["memory_uid"], job["memory_uid"])
        self.assertEqual(responder_kwargs["persona_profile_id"], job["persona_profile_id"])

    async def test_job_sets_knowledge_owner_id_to_api_key_when_persona_scoped_to_user(self):
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        old_value = getattr(conversation.settings, "API_PERSONA_PER_KEY", True)
        setattr(conversation.settings, "API_PERSONA_PER_KEY", False)
        try:
            job, _ = await self._run_endpoint(payload)
        finally:
            setattr(conversation.settings, "API_PERSONA_PER_KEY", old_value)

        self.assertEqual(job["persona_owner_id"], 1)
        self.assertEqual(job["knowledge_owner_id"], 2)


    async def test_user_id_whitespace_rejected_before_worker_call(self):
        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_job_mock = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(conversation, "_send_job_and_wait", send_job_mock):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "   ", "message": "hi"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"].get("code"), "invalid_payload")
        send_job_mock.assert_not_called()

    async def test_user_id_normalized_by_trimming(self):
        payload = conversation.ConversationRequest(user_id="  user-1  ", message="hi")

        self.assertEqual(payload.user_id, "user-1")

    async def test_image_payload_with_only_whitespace_rejected(self):
        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_job_mock = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(conversation, "_send_job_and_wait", send_job_mock):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={
                    "user_id": "user-1",
                    "image_b64": "   \n\t  ",
                    "image_mime": "image/png",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"].get("code"), "invalid_payload")
        send_job_mock.assert_not_called()

    async def test_voice_payload_with_only_whitespace_rejected(self):
        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_job_mock = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(conversation, "_send_job_and_wait", send_job_mock):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={
                    "user_id": "user-1",
                    "voice_b64": "\n  \t   ",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"].get("code"), "invalid_payload")
        send_job_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
