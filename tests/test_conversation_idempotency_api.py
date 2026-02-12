import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import conversation


class _SequenceRedis:
    def __init__(self, get_values=None, set_exception=None, set_result=True, set_values=None):
        self._get_values = list(get_values or [])
        self._set_exception = set_exception
        self._set_result = set_result
        self._set_values = list(set_values or [])

    async def get(self, _key):
        if self._get_values:
            return self._get_values.pop(0)
        return None

    async def set(self, *_args, **_kwargs):
        if self._set_values:
            value = self._set_values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        if self._set_exception is not None:
            raise self._set_exception
        return self._set_result


class ConversationIdempotencyApiTests(unittest.TestCase):
    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(conversation.router)
        app.dependency_overrides[conversation._auth_api_key] = lambda: {"user_id": 1, "id": 2}
        return TestClient(app)

    def test_idempotency_returns_503_when_storage_unavailable(self):
        with patch.object(conversation, "get_redis", return_value=None):
            client = self._client()
            response = client.post(
                "/api/v1/conversation",
                headers={"Idempotency-Key": "idem-1"},
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "idempotency_unavailable")

    def test_idempotency_returns_503_when_lock_operation_fails_both_attempts(self):
        redis = _SequenceRedis(
            get_values=[None, None, None],
            set_values=[RuntimeError("redis down"), RuntimeError("redis down")],
        )
        send_job = AsyncMock()
        session_scope = AsyncMock()

        with (
            patch.object(conversation, "get_redis", return_value=redis),
            patch.object(conversation, "_send_job_and_wait", new=send_job),
            patch.object(conversation, "session_scope", new=session_scope),
        ):
            client = self._client()
            response = client.post(
                "/api/v1/conversation",
                headers={"Idempotency-Key": "idem-1"},
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "idempotency_unavailable")
        send_job.assert_not_awaited()
        session_scope.assert_not_awaited()

    def test_idempotency_returns_503_when_lock_not_acquired_without_exception(self):
        redis = _SequenceRedis(get_values=[None, None, None], set_values=[False, False])
        send_job = AsyncMock()
        session_scope = AsyncMock()

        with (
            patch.object(conversation, "get_redis", return_value=redis),
            patch.object(conversation, "_send_job_and_wait", new=send_job),
            patch.object(conversation, "session_scope", new=session_scope),
        ):
            client = self._client()
            response = client.post(
                "/api/v1/conversation",
                headers={"Idempotency-Key": "idem-1"},
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "idempotency_unavailable")
        send_job.assert_not_awaited()
        session_scope.assert_not_awaited()

    def test_idempotency_returns_503_for_mixed_lock_failures_between_attempts(self):
        variants = [
            [False, RuntimeError("redis down")],
            [RuntimeError("redis down"), False],
        ]

        for set_values in variants:
            with self.subTest(set_values=set_values):
                redis = _SequenceRedis(get_values=[None, None, None], set_values=list(set_values))
                send_job = AsyncMock()
                session_scope = AsyncMock()

                with (
                    patch.object(conversation, "get_redis", return_value=redis),
                    patch.object(conversation, "_send_job_and_wait", new=send_job),
                    patch.object(conversation, "session_scope", new=session_scope),
                ):
                    client = self._client()
                    response = client.post(
                        "/api/v1/conversation",
                        headers={"Idempotency-Key": "idem-1"},
                        json={"user_id": "u-1", "message": "hi"},
                    )

                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["code"], "idempotency_unavailable")
                send_job.assert_not_awaited()
                session_scope.assert_not_awaited()

    def test_idempotency_keeps_inflight_409_behavior(self):
        redis = _SequenceRedis(get_values=["inflight:1710000000"])

        with patch.object(conversation, "get_redis", return_value=redis):
            client = self._client()
            response = client.post(
                "/api/v1/conversation",
                headers={"Idempotency-Key": "idem-1"},
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "idempotency_in_flight")

    def test_idempotency_keeps_cached_replay_behavior(self):
        cached = json.dumps(
            {
                "status_code": 200,
                "body": {
                    "reply": "cached",
                    "latency_ms": 7,
                    "latency_breakdown": {
                        "queue_latency_ms": 0,
                        "worker_latency_ms": 0,
                        "total_latency_ms": 7,
                    },
                    "request_id": "req-cached",
                },
            }
        )
        redis = _SequenceRedis(get_values=[cached])

        with patch.object(conversation, "get_redis", return_value=redis):
            client = self._client()
            response = client.post(
                "/api/v1/conversation",
                headers={"Idempotency-Key": "idem-1"},
                json={"user_id": "u-1", "message": "hi"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["reply"], "cached")
        self.assertEqual(payload["request_id"], "req-cached")

    def test_without_idempotency_key_behavior_is_unchanged(self):
        with (
            patch.object(conversation, "get_redis", return_value=None),
            patch.object(conversation, "_check_rate_limit", new=AsyncMock()),
            patch.object(
                conversation,
                "_send_job_and_wait",
                new=AsyncMock(return_value={"ok": True, "reply": "ok", "request_id": "req-1"}),
            ),
            patch.object(conversation, "register_api_memory_uid", new=AsyncMock()),
            patch.object(conversation, "inc_stats", new=AsyncMock()),
            patch.object(conversation, "update_cached_personas_for_owner", new=AsyncMock()),
        ):
            class _Result:
                def scalar_one_or_none(self):
                    return "free"

            class _Db:
                async def execute(self, _stmt):
                    return _Result()

            @asynccontextmanager
            async def _fake_session_scope(**_kwargs):
                yield _Db()

            with patch.object(conversation, "session_scope", _fake_session_scope):
                client = self._client()
                response = client.post(
                    "/api/v1/conversation",
                    json={"user_id": "u-1", "message": "hi"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "ok")


if __name__ == "__main__":
    unittest.main()
