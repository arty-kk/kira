import unittest
from unittest.mock import patch

import httpx

from app.clients import openai_client
from tenacity import wait_fixed


class _FakeTranscriptions:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        result = self._outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeClient:
    def __init__(self, outcomes):
        self.audio = type("Audio", (), {"transcriptions": _FakeTranscriptions(outcomes)})()


class OpenAITranscribeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_only_for_retryable_errors(self) -> None:
        client = _FakeClient([
            httpx.ReadTimeout("t1"),
            httpx.ReadTimeout("t2"),
            "ok",
        ])
        with patch.object(openai_client, "get_openai", return_value=client), \
             patch.object(openai_client, "OPENAI_MAX_ATTEMPTS", 3), \
             patch.object(openai_client, "wait_exponential", return_value=wait_fixed(0)):
            resp = await openai_client.transcribe_audio_with_retry(model="whisper-1", file=b"x")
        self.assertEqual(resp, "ok")
        self.assertEqual(client.audio.transcriptions.calls, 3)

    async def test_does_not_retry_for_non_retryable_errors(self) -> None:
        client = _FakeClient([ValueError("bad payload")])
        with patch.object(openai_client, "get_openai", return_value=client), \
             patch.object(openai_client, "OPENAI_MAX_ATTEMPTS", 5), \
             patch.object(openai_client, "wait_exponential", return_value=wait_fixed(0)):
            with self.assertRaises(ValueError):
                await openai_client.transcribe_audio_with_retry(model="whisper-1", file=b"x")
        self.assertEqual(client.audio.transcriptions.calls, 1)


if __name__ == "__main__":
    unittest.main()
