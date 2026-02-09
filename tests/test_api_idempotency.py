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


if __name__ == "__main__":
    unittest.main()
