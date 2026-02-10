import unittest
import unittest.mock
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import conversation


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
        register_memory_uid = register_mock.await_args.args[1]
        return job, register_memory_uid

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


if __name__ == "__main__":
    unittest.main()
