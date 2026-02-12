import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import conversation


class _DummyResult:
    def __init__(self, *, scalar_value=None):
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value


class _FakeDB:
    def __init__(self, outbox_rows):
        self._outbox_rows = outbox_rows
        self._execute_calls = 0

    async def execute(self, _stmt):
        self._execute_calls += 1
        if self._execute_calls == 1:
            return _DummyResult(scalar_value="free")
        return _DummyResult()

    def add(self, row):
        row.id = len(self._outbox_rows) + 1
        self._outbox_rows.append(row)

    async def flush(self):
        return None


class ConversationRefundOutboxTests(unittest.TestCase):
    def test_duplicate_worker_error_keeps_primary_error_without_refund_outbox(self):
        outbox_rows = []

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB(outbox_rows)

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_task_mock = AsyncMock(return_value={
            "ok": False,
            "error": {
                "status": 409,
                "code": "duplicate_request",
                "message": "duplicate",
            },
        })
        refund_mock = AsyncMock()

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(conversation, "_send_job_and_wait", new=send_task_mock),
            patch.object(conversation, "_refund_request", new=refund_mock),
            patch.object(conversation, "get_redis", return_value=None),
            patch.object(conversation.asyncio, "sleep", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 409)
        payload = response.json().get("detail") or {}
        self.assertEqual(payload.get("code"), "duplicate_request")
        refund_mock.assert_not_awaited()
        self.assertEqual(outbox_rows, [])

    def test_worker_error_keeps_primary_error_and_stores_refund_outbox(self):
        outbox_rows = []

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB(outbox_rows)

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_task_mock = AsyncMock(return_value={
            "ok": False,
            "error": {
                "status": 400,
                "code": "invalid_payload",
                "message": "bad payload",
            },
        })

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(conversation, "_send_job_and_wait", new=send_task_mock),
            patch.object(conversation, "_refund_request", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch.object(conversation, "get_redis", return_value=None),
            patch.object(conversation.asyncio, "sleep", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json().get("detail") or {}
        self.assertEqual(payload.get("code"), "invalid_payload")

        self.assertEqual(len(outbox_rows), 1)
        outbox = outbox_rows[0]
        self.assertEqual(outbox.owner_id, 1)
        self.assertEqual(outbox.billing_tier, "free")
        self.assertTrue(str(outbox.request_id))
        self.assertEqual(outbox.reason, "worker_error:invalid_payload")
        self.assertEqual(outbox.attempts, 3)
        self.assertIn("RuntimeError", str(outbox.last_error))

    def test_invalid_voice_format_worker_error_stores_refund_outbox(self):
        outbox_rows = []

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB(outbox_rows)

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_task_mock = AsyncMock(return_value={
            "ok": False,
            "error": {
                "status": 400,
                "code": "invalid_voice_format",
                "message": "unknown voice format",
            },
        })

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(conversation, "_send_job_and_wait", new=send_task_mock),
            patch.object(conversation, "_refund_request", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch.object(conversation, "get_redis", return_value=None),
            patch.object(conversation.asyncio, "sleep", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json().get("detail") or {}
        self.assertEqual(payload.get("code"), "invalid_voice_format")

        self.assertEqual(len(outbox_rows), 1)
        outbox = outbox_rows[0]
        self.assertEqual(outbox.reason, "worker_error:invalid_voice_format")


    def test_refund_compensation_failure_returns_dedicated_500(self):
        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB([])

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        send_task_mock = AsyncMock(return_value={
            "ok": False,
            "error": {
                "status": 400,
                "code": "invalid_payload",
                "message": "bad payload",
            },
        })

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(conversation, "_send_job_and_wait", new=send_task_mock),
            patch.object(conversation, "_refund_request", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch.object(conversation, "_store_refund_outbox_task", new=AsyncMock(return_value=None)),
            patch.object(conversation, "get_redis", return_value=None),
            patch.object(conversation.asyncio, "sleep", new=AsyncMock()),
            patch.object(conversation.logger, "critical") as critical_mock,
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 500)
        payload = response.json().get("detail") or {}
        self.assertEqual(
            payload,
            {
                "code": "refund_compensation_failed",
                "message": "Request failed with a risk of billing desynchronization.",
                "request_id": payload.get("request_id"),
            },
        )
        self.assertTrue(str(payload.get("request_id") or ""))

        critical_mock.assert_called_once()
        critical_msg, request_id, owner_id, original_error_code = critical_mock.call_args.args
        self.assertEqual(
            critical_msg,
            "Refund compensation failed request_id=%s owner_id=%s original_error_code=%s",
        )
        self.assertTrue(str(request_id))
        self.assertEqual(owner_id, 1)
        self.assertEqual(original_error_code, "invalid_payload")

    def test_success_path_returns_only_original_result_without_refund_outbox(self):
        outbox_rows = []

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB(outbox_rows)

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        refund_mock = AsyncMock()

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(
                conversation,
                "_send_job_and_wait",
                new=AsyncMock(return_value={"ok": True, "reply": "only-original", "request_id": "rid-1"}),
            ),
            patch.object(conversation, "_refund_request", new=refund_mock),
            patch.object(conversation, "get_redis", return_value=None),
            patch.object(conversation.asyncio, "sleep", new=AsyncMock()),
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("reply"), "only-original")
        self.assertNotIn("duplicate_request", str(payload))
        refund_mock.assert_not_awaited()
        self.assertEqual(outbox_rows, [])


if __name__ == "__main__":
    unittest.main()
