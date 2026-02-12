from contextlib import asynccontextmanager
import json
import unittest
import unittest.mock

from starlette.requests import Request

from app.api import conversation


class _DummyRedis:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def get(self, _key):
        return self._payload




class _ExpiringFakeRedis:
    def __init__(self, now_fn) -> None:
        self._now_fn = now_fn
        self._data = {}
        self.set_calls = []
        self.delete_calls = []

    def _cleanup(self, key: str) -> None:
        record = self._data.get(key)
        if record is None:
            return
        value, expires_at = record
        if expires_at is not None and self._now_fn() >= expires_at:
            self._data.pop(key, None)

    async def get(self, key):
        self._cleanup(key)
        record = self._data.get(key)
        if record is None:
            return None
        value, _expires_at = record
        return value

    async def set(self, key, value, nx=False, ex=None):
        self._cleanup(key)
        self.set_calls.append({"key": key, "nx": nx, "ex": ex, "value": value})
        if nx and key in self._data:
            return False
        expires_at = None if ex is None else self._now_fn() + int(ex)
        self._data[key] = (value, expires_at)
        return True

    async def delete(self, key):
        self.delete_calls.append(key)
        self._data.pop(key, None)


class ApiIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_api_key_falls_back_to_x_api_key_for_non_bearer_auth(self) -> None:
        @asynccontextmanager
        async def _fake_session_scope(*_args, **_kwargs):
            yield object()

        api_key_obj = unittest.mock.Mock(id=10, user_id=20)

        with (
            unittest.mock.patch.object(conversation, "session_scope", _fake_session_scope),
            unittest.mock.patch.object(
                conversation,
                "authenticate_key",
                new=unittest.mock.AsyncMock(return_value=api_key_obj),
            ) as auth_mock,
        ):
            result = await conversation._auth_api_key(
                x_api_key="  fallback-key  ",
                authorization="Basic abc",
            )

        self.assertEqual(result, {"id": 10, "user_id": 20})
        self.assertEqual(auth_mock.await_count, 1)
        self.assertEqual(auth_mock.await_args.args[1], "fallback-key")

    async def test_auth_api_key_rejects_empty_bearer_token(self) -> None:
        with self.assertRaises(conversation.HTTPException) as exc:
            await conversation._auth_api_key(x_api_key=None, authorization="Bearer ")

        self.assertEqual(exc.exception.status_code, 401)
        self.assertEqual(exc.exception.detail.get("code"), "missing_api_key")

    def test_normalize_idempotency_key_trim_and_empty_behavior(self) -> None:
        self.assertEqual(conversation._normalize_idempotency_key("  abc  "), "abc")
        self.assertIsNone(conversation._normalize_idempotency_key("   "))
        self.assertIsNone(conversation._normalize_idempotency_key(None))

    def test_idempotency_long_keys_with_same_prefix_do_not_collapse(self) -> None:
        shared_prefix = "x" * 128
        key_a = shared_prefix + "A" * 30
        key_b = shared_prefix + "B" * 30

        normalized_a = conversation._normalize_idempotency_key(key_a)
        normalized_b = conversation._normalize_idempotency_key(key_b)

        self.assertNotEqual(normalized_a, normalized_b)
        self.assertNotEqual(
            conversation._idempotency_redis_key(1, normalized_a),
            conversation._idempotency_redis_key(1, normalized_b),
        )

    def test_normalize_idempotency_key_max_length_boundaries(self) -> None:
        self.assertEqual(
            conversation._normalize_idempotency_key("k" * 256),
            "k" * 256,
        )

        with self.assertRaises(conversation.HTTPException) as exc:
            conversation._normalize_idempotency_key("k" * 257)

        self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(exc.exception.detail.get("code"), "invalid_idempotency_key")

    async def test_idempotency_returns_cached_response(self) -> None:
        cached = json.dumps(
            {
                "status_code": 200,
                "body": {
                    "reply": "ok",
                    "latency_ms": 12,
                    "latency_breakdown": {
                        "queue_latency_ms": 0,
                        "worker_latency_ms": 0,
                        "total_latency_ms": 12,
                    },
                    "request_id": "req-1",
                },
            }
        )
        dummy_redis = _DummyRedis(cached)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/conversation",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        request = Request(scope)
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        with unittest.mock.patch.object(conversation, "get_redis", return_value=dummy_redis):
            resp = await conversation.conversation_endpoint(
                payload,
                request,
                api_key={"user_id": 1, "id": 2},
                idempotency_key="abc",
            )

        self.assertEqual(resp.reply, "ok")
        self.assertEqual(resp.request_id, "req-1")


    def test_idempotency_hash_normalizes_message_whitespace(self) -> None:
        payload_a = conversation.ConversationRequest(user_id="user-1", message="hi")
        payload_b = conversation.ConversationRequest(user_id="user-1", message="  hi  ")

        hash_a = conversation._build_idempotency_request_hash(payload_a)
        hash_b = conversation._build_idempotency_request_hash(payload_b)

        self.assertEqual(hash_a, hash_b)

    async def test_idempotency_reused_key_with_different_payload_returns_409(self) -> None:
        cached_payload = conversation.ConversationRequest(user_id="user-1", message="first")
        cached = json.dumps(
            {
                "status_code": 200,
                "body": {
                    "reply": "ok",
                    "latency_ms": 12,
                    "latency_breakdown": {
                        "queue_latency_ms": 0,
                        "worker_latency_ms": 0,
                        "total_latency_ms": 12,
                    },
                    "request_id": "req-1",
                },
                "request_hash": conversation._build_idempotency_request_hash(cached_payload),
            }
        )
        dummy_redis = _DummyRedis(cached)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/conversation",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        request = Request(scope)
        payload = conversation.ConversationRequest(user_id="user-1", message="second")

        with unittest.mock.patch.object(conversation, "get_redis", return_value=dummy_redis):
            with self.assertRaises(conversation.HTTPException) as exc:
                await conversation.conversation_endpoint(
                    payload,
                    request,
                    api_key={"user_id": 1, "id": 2},
                    idempotency_key="abc",
                )

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(
            exc.exception.detail.get("code"),
            "idempotency_key_reused_with_different_payload",
        )

    async def test_conversation_rejects_too_long_idempotency_key_with_400(self) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/conversation",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        request = Request(scope)
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        with self.assertRaises(conversation.HTTPException) as exc:
            await conversation.conversation_endpoint(
                payload,
                request,
                api_key={"user_id": 1, "id": 2},
                idempotency_key="k" * 257,
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(exc.exception.detail.get("code"), "invalid_idempotency_key")


    async def test_inflight_lock_uses_short_ttl_and_expires_after_crash(self) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/conversation",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        request = Request(scope)
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        now = {"value": 1000.0}
        fake_redis = _ExpiringFakeRedis(lambda: now["value"])

        original_ttl = conversation.settings.API_IDEMPOTENCY_TTL_SEC
        original_inflight_ttl = conversation.settings.API_IDEMPOTENCY_INFLIGHT_TTL_SEC
        conversation.settings.API_IDEMPOTENCY_TTL_SEC = 3600
        conversation.settings.API_IDEMPOTENCY_INFLIGHT_TTL_SEC = 2
        try:
            with (
                unittest.mock.patch.object(conversation, "get_redis", return_value=fake_redis),
                unittest.mock.patch.object(conversation, "_check_rate_limit", side_effect=RuntimeError("boom")),
                unittest.mock.patch.object(conversation.time, "time", side_effect=lambda: now["value"]),
            ):
                with self.assertRaises(RuntimeError):
                    await conversation.conversation_endpoint(
                        payload,
                        request,
                        api_key={"user_id": 1, "id": 2},
                        idempotency_key="abc",
                    )

                with self.assertRaises(conversation.HTTPException) as inflight_exc:
                    await conversation.conversation_endpoint(
                        payload,
                        request,
                        api_key={"user_id": 1, "id": 2},
                        idempotency_key="abc",
                    )

                self.assertEqual(inflight_exc.exception.status_code, 409)
                self.assertEqual(inflight_exc.exception.detail.get("code"), "idempotency_in_flight")

                now["value"] += 3

                with self.assertRaises(RuntimeError):
                    await conversation.conversation_endpoint(
                        payload,
                        request,
                        api_key={"user_id": 1, "id": 2},
                        idempotency_key="abc",
                    )
        finally:
            conversation.settings.API_IDEMPOTENCY_TTL_SEC = original_ttl
            conversation.settings.API_IDEMPOTENCY_INFLIGHT_TTL_SEC = original_inflight_ttl

        inflight_set_calls = [call for call in fake_redis.set_calls if call["nx"] is True]
        self.assertGreaterEqual(len(inflight_set_calls), 2)
        self.assertEqual(inflight_set_calls[0]["ex"], 2)
        self.assertTrue(all(call["ex"] != 3600 for call in inflight_set_calls))

    async def test_malformed_idempotency_record_is_deleted_and_request_continues(self) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/conversation",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
        request = Request(scope)
        payload = conversation.ConversationRequest(user_id="user-1", message="hi")

        now = {"value": 1000.0}
        fake_redis = _ExpiringFakeRedis(lambda: now["value"])
        idem_key = conversation._idempotency_redis_key(2, "broken")
        fake_redis._data[idem_key] = ("not-json", None)

        check_rate_limit_error = RuntimeError("rate-limit-called")
        original_inflight_ttl = conversation.settings.API_IDEMPOTENCY_INFLIGHT_TTL_SEC
        conversation.settings.API_IDEMPOTENCY_INFLIGHT_TTL_SEC = 1
        try:
            with (
                unittest.mock.patch.object(conversation, "get_redis", return_value=fake_redis),
                unittest.mock.patch.object(
                    conversation,
                    "_check_rate_limit",
                    side_effect=check_rate_limit_error,
                ) as check_mock,
                unittest.mock.patch.object(conversation.time, "time", side_effect=lambda: now["value"]),
            ):
                with self.assertRaises(RuntimeError) as first_exc:
                    await conversation.conversation_endpoint(
                        payload,
                        request,
                        api_key={"user_id": 1, "id": 2},
                        idempotency_key="broken",
                    )

                self.assertIs(first_exc.exception, check_rate_limit_error)
                self.assertIn(idem_key, fake_redis.delete_calls)
                self.assertTrue((await fake_redis.get(idem_key)).startswith("inflight:"))

                now["value"] += 2

                with self.assertRaises(RuntimeError) as second_exc:
                    await conversation.conversation_endpoint(
                        payload,
                        request,
                        api_key={"user_id": 1, "id": 2},
                        idempotency_key="broken",
                    )

                self.assertIs(second_exc.exception, check_rate_limit_error)

            self.assertEqual(check_mock.await_count, 2)
            inflight_set_calls = [call for call in fake_redis.set_calls if call["nx"] is True]
            self.assertGreaterEqual(len(inflight_set_calls), 2)
        finally:
            conversation.settings.API_IDEMPOTENCY_INFLIGHT_TTL_SEC = original_inflight_ttl


if __name__ == "__main__":
    unittest.main()
