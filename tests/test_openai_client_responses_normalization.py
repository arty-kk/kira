import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tenacity import wait_fixed

from app.clients import openai_client


class OpenAIResponsesNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gpt_52_keeps_sampling_and_sets_none_effort(self) -> None:
        captured = {}

        class _FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(usage={"total_tokens": 1})

        client = SimpleNamespace(responses=_FakeResponses())
        with patch.object(openai_client, "get_openai", return_value=client), patch.object(
            openai_client, "OPENAI_MAX_ATTEMPTS", 1
        ), patch.object(openai_client, "wait_exponential", return_value=wait_fixed(0)):
            await openai_client._call_openai_with_retry(
                endpoint="responses.create",
                model="gpt-5.2",
                model_role="regular",
                input="hello",
                temperature=0.7,
                top_p=0.9,
                presence_penalty=0.1,
                frequency_penalty=0.2,
                max_output_tokens=32,
            )

        self.assertEqual(captured.get("temperature"), 0.7)
        self.assertEqual(captured.get("top_p"), 0.9)
        self.assertNotIn("presence_penalty", captured)
        self.assertNotIn("frequency_penalty", captured)
        self.assertEqual((captured.get("reasoning") or {}).get("effort"), "none")
        self.assertEqual((captured.get("text") or {}).get("verbosity"), "low")


    async def test_gpt_52_real_spec_payload_is_preserved(self) -> None:
        captured = {}

        class _FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(usage={"total_tokens": 1})

        client = SimpleNamespace(responses=_FakeResponses())
        with patch.object(openai_client, "get_openai", return_value=client), patch.object(
            openai_client, "OPENAI_MAX_ATTEMPTS", 1
        ), patch.object(openai_client, "wait_exponential", return_value=wait_fixed(0)):
            await openai_client._call_openai_with_retry(
                endpoint="responses.create",
                model="gpt-5.2",
                reasoning={"effort": "none"},
                text={"verbosity": "low"},
                temperature=0.7,
                top_p=0.9,
                max_output_tokens=1000,
                truncation="auto",
                store=False,
                instructions="Ты AI-персона. Соблюдай стиль, ограничения и характер.",
                input=[
                    {
                        "role": "developer",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Динамический контекст: профиль пользователя, память, цели, ограничения, текущая сцена.",
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Сгенерируй финальный ответ пользователю.",
                            }
                        ],
                    },
                ],
            )

        self.assertEqual(captured.get("temperature"), 0.7)
        self.assertEqual(captured.get("top_p"), 0.9)
        self.assertEqual(captured.get("reasoning"), {"effort": "none"})
        self.assertEqual((captured.get("text") or {}).get("verbosity"), "low")
        self.assertEqual(captured.get("truncation"), "auto")
        self.assertFalse(captured.get("store"))
        self.assertEqual((captured.get("input") or [])[0].get("role"), "developer")

    async def test_non_gpt_52_strips_sampling_and_keeps_legacy_effort_logic(self) -> None:
        captured = {}

        class _FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(usage={"total_tokens": 1})

        client = SimpleNamespace(responses=_FakeResponses())
        with patch.object(openai_client, "get_openai", return_value=client), patch.object(
            openai_client, "OPENAI_MAX_ATTEMPTS", 1
        ), patch.object(openai_client, "wait_exponential", return_value=wait_fixed(0)):
            await openai_client._call_openai_with_retry(
                endpoint="responses.create",
                model="gpt-5-mini",
                model_role="regular",
                input="hello",
                temperature=0.7,
                top_p=0.9,
                presence_penalty=0.1,
                frequency_penalty=0.2,
                max_output_tokens=32,
            )

        self.assertNotIn("temperature", captured)
        self.assertNotIn("top_p", captured)
        self.assertNotIn("presence_penalty", captured)
        self.assertNotIn("frequency_penalty", captured)
        self.assertEqual((captured.get("reasoning") or {}).get("effort"), "low")


if __name__ == "__main__":
    unittest.main()
