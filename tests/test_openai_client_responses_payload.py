import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tenacity import wait_fixed

from app.clients import openai_client


class OpenAIResponsesPayloadTests(unittest.IsolatedAsyncioTestCase):
    def test_canonicalizes_same_semantics_to_same_payload(self) -> None:
        p1 = openai_client._prepare_responses_payload(
            prompt_profile="test.profile",
            instructions="sys",
            input="hello",
            model="gpt-5-mini",
        )
        p2 = openai_client._prepare_responses_payload(
            prompt_profile="test.profile",
            instructions="sys",
            input=[{"role": "user", "content": "hello"}],
            model="gpt-5-mini",
        )
        self.assertEqual(p1, p2)

    def test_static_prefix_and_dynamic_suffix_are_separated(self) -> None:
        payload = openai_client._prepare_responses_payload(
            prompt_profile="test.profile",
            static_prefix="STATIC:",
            dynamic_suffix=" DYNAMIC",
            instructions="legacy",
            input="hello",
            model="gpt-5-mini",
        )
        self.assertEqual(payload["instructions"], "STATIC: DYNAMIC")

    async def test_logs_prompt_profile_without_prompt_content(self) -> None:
        captured = {}

        class _FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(usage={"total_tokens": 1})

        client = SimpleNamespace(responses=_FakeResponses())
        with patch.object(openai_client, "get_openai", return_value=client), patch.object(
            openai_client, "OPENAI_MAX_ATTEMPTS", 1
        ), patch.object(openai_client, "wait_exponential", return_value=wait_fixed(0)):
            with self.assertLogs(openai_client.logger, level="INFO") as logs:
                await openai_client._call_openai_with_retry(
                    endpoint="responses.create",
                    model="gpt-5-mini",
                    model_role="regular",
                    prompt_profile="unit.profile",
                    instructions="SECRET_SYSTEM",
                    input="SECRET_USER",
                    max_output_tokens=32,
                )

        merged = "\n".join(logs.output)
        self.assertIn("prompt_profile", merged)
        self.assertIn("unit.profile", merged)
        self.assertNotIn("SECRET_SYSTEM", merged)
        self.assertNotIn("SECRET_USER", merged)
        self.assertNotIn("prompt_profile", captured)


if __name__ == "__main__":
    unittest.main()
