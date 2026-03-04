import unittest

from app import prompts_base


class CorefPromptGuardrailsTests(unittest.TestCase):
    def test_rewrite_prompt_is_language_agnostic_and_plain_text_output(self) -> None:
        self.assertIn("1. Система должна быть агностична к языку", prompts_base.COREF_REWRITE_PROMPT)
        self.assertIn("2. Сохраняй язык текущего сообщения пользователя.", prompts_base.COREF_REWRITE_PROMPT)
        self.assertIn("Теперь верни только запрос (переписанный или исходный). Без пояснений и дополнительного текста.", prompts_base.COREF_REWRITE_PROMPT)
        self.assertNotIn('Output JSON ONLY: {"rewritten": string}.', prompts_base.COREF_REWRITE_PROMPT)

    def test_extract_prompt_kept_as_legacy_archive(self) -> None:
        self.assertIn("NEVER add new facts", prompts_base.COREF_EXTRACT_PROMPT)


if __name__ == "__main__":
    unittest.main()
