import unittest

from app import prompts_base


class CorefPromptGuardrailsTests(unittest.TestCase):
    def test_needs_coref_prompt_handles_short_confirmations(self) -> None:
        self.assertIn("confirmations", prompts_base.COREF_SYSTEM_PROMPT)
        self.assertIn("давай", prompts_base.COREF_SYSTEM_PROMPT)

    def test_extract_prompt_forbids_fact_injection(self) -> None:
        self.assertIn("NEVER add new facts", prompts_base.COREF_EXTRACT_PROMPT)
        self.assertIn("confirmations", prompts_base.COREF_EXTRACT_PROMPT)


class NeedsCorefPromptPayloadTests(unittest.TestCase):
    def test_build_user_prompt_includes_snippet_context(self) -> None:
        from app.services.responder.coref.needs_coref import _build_user_prompt

        prompt = _build_user_prompt(
            "давай",
            history=[
                {"role": "user", "content": "хочу купить юси"},
                {"role": "assistant", "content": "могу скинуть ссылку"},
            ],
        )
        self.assertIn("SNIPPET:", prompt)
        self.assertIn("могу скинуть ссылку", prompt)
        self.assertIn("QUERY", prompt)


if __name__ == "__main__":
    unittest.main()
