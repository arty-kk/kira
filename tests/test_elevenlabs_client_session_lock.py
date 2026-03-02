import asyncio
import unittest
from unittest.mock import patch

from app.clients.elevenlabs_client import ElevenLabsClient


class _FakeResponse:
    def __init__(self, payload: bytes = b"ok"):
        self.status = 200
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def read(self) -> bytes:
        return self._payload

    async def text(self) -> str:
        return self._payload.decode("utf-8", "ignore")


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.close_calls = 0
        self.post_calls = 0

    def post(self, *_args, **_kwargs):
        self.post_calls += 1
        return _FakeResponse()

    async def close(self):
        self.close_calls += 1
        self.closed = True


class ElevenLabsClientSessionLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_session_concurrent_singleton(self):
        created = []

        def _session_ctor(*_args, **_kwargs):
            s = _FakeSession()
            created.append(s)
            return s

        client = ElevenLabsClient(api_key="k")
        with patch("app.clients.elevenlabs_client.aiohttp.ClientSession", side_effect=_session_ctor):
            sessions = await asyncio.gather(*[client._get_session() for _ in range(20)])

        self.assertEqual(len(created), 1)
        self.assertTrue(all(s is created[0] for s in sessions))

    async def test_synthesize_concurrent_uses_one_session(self):
        created = []

        def _session_ctor(*_args, **_kwargs):
            s = _FakeSession()
            created.append(s)
            return s

        client = ElevenLabsClient(api_key="k")
        with patch("app.clients.elevenlabs_client.aiohttp.ClientSession", side_effect=_session_ctor):
            results = await asyncio.gather(
                *[
                    client.synthesize(text="hi", lang="en", voice_id="voice")
                    for _ in range(10)
                ]
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(results, [b"ok"] * 10)
        self.assertEqual(created[0].post_calls, 10)

    async def test_close_idempotent_and_reopen_creates_new_session(self):
        created = []

        def _session_ctor(*_args, **_kwargs):
            s = _FakeSession()
            created.append(s)
            return s

        client = ElevenLabsClient(api_key="k")
        with patch("app.clients.elevenlabs_client.aiohttp.ClientSession", side_effect=_session_ctor):
            first = await client._get_session()
            await client.close()
            await client.close()
            second = await client._get_session()

        self.assertIsNot(first, second)
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].close_calls, 1)
        self.assertFalse(created[1].closed)


if __name__ == "__main__":
    unittest.main()
