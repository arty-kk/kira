import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
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
        self._last_lookup_key = None

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM users" in sql:
            return _DummyResult(scalar_value="free")

        if "INSERT INTO refund_outbox" in sql:
            params = stmt.compile().params
            key = str(params["request_id"])
            self._last_lookup_key = key
            existing = next((row for row in self._outbox_rows if row.request_id == key), None)
            if existing is not None:
                return _DummyResult(scalar_value=None)

            row = SimpleNamespace(
                id=len(self._outbox_rows) + 1,
                owner_id=params["owner_id"],
                billing_tier=params["billing_tier"],
                request_id=key,
                reason=str(params["reason"]),
                status=params["status"],
                attempts=params["attempts"],
                last_error=params["last_error"],
            )
            self._outbox_rows.append(row)
            return _DummyResult(scalar_value=row.id)

        if "SELECT refund_outbox.id" in sql:
            key = self._last_lookup_key
            existing = next((row for row in self._outbox_rows if row.request_id == key), None)
            return _DummyResult(scalar_value=(existing.id if existing else None))

        return _DummyResult()


class ConversationRefundOutboxTests(unittest.TestCase):
    def _run_case(
        self,
        *,
        source,
        status_code,
        code,
        message,
    ):
        outbox_rows = []

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB(outbox_rows)

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        if source == "http":
            send_task_mock = AsyncMock(
                side_effect=HTTPException(
                    status_code=status_code,
                    detail={"code": code, "message": message},
                )
            )
        elif source == "timeout":
            send_task_mock = AsyncMock(side_effect=asyncio.TimeoutError())
        else:
            send_task_mock = AsyncMock(
                return_value={
                    "ok": False,
                    "error": {
                        "status": status_code,
                        "code": code,
                        "message": message,
                    },
                }
            )

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(conversation, "_send_job_and_wait", new=send_task_mock),
            patch.object(conversation, "get_redis", return_value=None),
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        return response, outbox_rows

    def test_duplicate_worker_error_keeps_primary_error_without_refund_outbox(self):
        response, outbox_rows = self._run_case(
            source="worker",
            status_code=409,
            code="duplicate_request",
            message="duplicate",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json().get("detail") or {}
        self.assertEqual(payload.get("code"), "duplicate_request")
        self.assertEqual(outbox_rows, [])

    def test_worker_error_keeps_primary_error_and_stores_refund_outbox(self):
        response, outbox_rows = self._run_case(
            source="worker",
            status_code=400,
            code="invalid_payload",
            message="bad payload",
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
        self.assertEqual(outbox.attempts, 0)

    def test_refund_decision_is_consistent_between_http_and_worker(self):
        cases = [
            ("invalid_voice_format", 400, True),
            ("voice_transcription_failed", 400, True),
            ("payload_too_large", 413, True),
            ("duplicate_request", 409, False),
        ]
        for code, status_code, should_refund in cases:
            for source in ("http", "worker"):
                with self.subTest(code=code, source=source, should_refund=should_refund):
                    response, outbox_rows = self._run_case(
                        source=source,
                        status_code=status_code,
                        code=code,
                        message=f"{code}-message",
                    )

                    self.assertEqual(response.status_code, status_code)
                    payload = response.json().get("detail") or {}
                    self.assertEqual(payload.get("code"), code)
                    self.assertEqual(payload.get("message"), f"{code}-message")
                    self.assertEqual(len(outbox_rows), 1 if should_refund else 0)

    def test_refundable_errors_store_outbox(self):
        cases = [
            ("http", 413, "payload_too_large", "http_exception:payload_too_large"),
            ("worker", 400, "invalid_voice_format", "worker_error:invalid_voice_format"),
        ]
        for source, status_code, code, expected_reason in cases:
            with self.subTest(source=source, code=code):
                response, outbox_rows = self._run_case(
                    source=source,
                    status_code=status_code,
                    code=code,
                    message=f"{code}-message",
                )

                self.assertEqual(response.status_code, status_code)
                payload = response.json().get("detail") or {}
                self.assertEqual(payload.get("code"), code)
                self.assertEqual(payload.get("message"), f"{code}-message")

                self.assertEqual(len(outbox_rows), 1)
                self.assertEqual(outbox_rows[0].reason, expected_reason)

    def test_timeout_refund_uses_canonical_upstream_timeout_and_preserves_error(self):
        response, outbox_rows = self._run_case(
            source="timeout",
            status_code=504,
            code="upstream_timeout",
            message="Model did not respond in time. Please retry.",
        )

        self.assertEqual(response.status_code, 504)
        payload = response.json().get("detail") or {}
        self.assertEqual(payload.get("code"), "upstream_timeout")
        self.assertEqual(payload.get("message"), "Model did not respond in time. Please retry.")

        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].reason, "timeout")

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
            patch.object(conversation, "_store_refund_outbox_task", new=AsyncMock(return_value=(None, False))),
            patch.object(conversation, "get_redis", return_value=None),
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

        critical_mock.assert_called_once()

    def test_success_path_returns_only_original_result_without_refund_outbox(self):
        outbox_rows = []

        @asynccontextmanager
        async def _fake_session_scope(**_kwargs):
            yield _FakeDB(outbox_rows)

        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}

        with (
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(conversation, "session_scope", _fake_session_scope),
            patch.object(
                conversation,
                "_send_job_and_wait",
                new=AsyncMock(return_value={"ok": True, "reply": "only-original", "request_id": "rid-1"}),
            ),
            patch.object(conversation, "get_redis", return_value=None),
        ):
            client = TestClient(app)
            response = client.post(
                "/api/v1/conversation",
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("reply"), "only-original")
        self.assertEqual(outbox_rows, [])


if __name__ == "__main__":
    unittest.main()
