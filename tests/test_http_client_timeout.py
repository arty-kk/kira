import unittest
from unittest.mock import patch

from app.clients.http_client import HTTPClient


class _ResponseContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeResponse:
    def raise_for_status(self):
        return None

    async def read(self):
        return b"ok"


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _ResponseContext(_FakeResponse())


class HTTPClientTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_without_per_call_timeout_keeps_session_default(self):
        client = HTTPClient(timeout_sec=10)
        fake_session = _FakeSession()

        with patch.object(client, "_get_session", return_value=fake_session):
            payload = await client.get_bytes("https://example.com/test")

        self.assertEqual(payload, b"ok")
        self.assertEqual(len(fake_session.calls), 1)
        self.assertNotIn("timeout", fake_session.calls[0]["kwargs"])


if __name__ == "__main__":
    unittest.main()
