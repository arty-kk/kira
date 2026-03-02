# ruff: noqa: E402
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
    os.environ.setdefault("TWITTER_API_KEY", "test")
    os.environ.setdefault("TWITTER_API_SECRET", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
    os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
    os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")


_seed_env()

from app.emo_engine.persona.utils import text_analyzer


class TextAnalyzerPromptCtxTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_text_ctx_prompt_contains_text_and_sanitized_context(self) -> None:
        analyzer = text_analyzer.TextAnalyzer()
        current_text = "Current line should stay intact"
        ctx_dialog = "[12:34] User: hi\n[12:34:56] Assistant: hello"
        mock_call = AsyncMock(return_value=SimpleNamespace())

        with patch.object(text_analyzer, "_call_openai_with_retry", mock_call), patch.object(
            text_analyzer,
            "_get_output_text",
            return_value='{"valence":0,"arousal":0.4,"dominance":0.4}',
        ):
            await analyzer.analyze_text(current_text, ctx_dialog=ctx_dialog)

        passed_input = mock_call.await_args.kwargs["input"]

        self.assertIn("User last message:\nCurrent line should stay intact", passed_input)
        self.assertIn("Conversation context (oldest→newest):", passed_input)
        self.assertIn("[] User: hi", passed_input)
        self.assertIn("[] Assistant: hello", passed_input)
        self.assertNotIn("[12:34]", passed_input)
        self.assertNotIn("[12:34:56]", passed_input)
        self.assertEqual(passed_input.count(current_text), 1)

    async def test_analyze_text_ctx_prompt_timestamp_cleanup_equivalence(self) -> None:
        analyzer = text_analyzer.TextAnalyzer()
        current_text = "Current line should stay intact"
        scenarios = {
            "short": "[12:34] User: hi",
            "medium": "[12:34] User: hi\n[12:34:56] Assistant: hello\n[21:09] User: got it",
        }

        for name, ctx_dialog in scenarios.items():
            with self.subTest(name=name):
                mock_call = AsyncMock(return_value=SimpleNamespace())
                with patch.object(text_analyzer, "_call_openai_with_retry", mock_call), patch.object(
                    text_analyzer,
                    "_get_output_text",
                    return_value='{"valence":0,"arousal":0.4,"dominance":0.4}',
                ):
                    await analyzer.analyze_text(current_text, ctx_dialog=ctx_dialog)

                passed_input = mock_call.await_args.kwargs["input"]
                self.assertIn("[]", passed_input)
                self.assertNotIn("[12:34]", passed_input)
                self.assertNotIn("[12:34:56]", passed_input)
                self.assertNotIn("[21:09]", passed_input)


if __name__ == "__main__":
    unittest.main()
