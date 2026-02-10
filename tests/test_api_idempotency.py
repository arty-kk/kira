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


class ApiIdempotencyTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
