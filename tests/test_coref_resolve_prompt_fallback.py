import unittest
from unittest.mock import AsyncMock, patch

import importlib

coref_mod = importlib.import_module("app.services.responder.coref.resolve_coref")


class CorefResolvePromptFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_coref_rewrites_via_single_prompt_plain_text(self) -> None:
        history = [
            {"role": "user", "content": "хочу купить юси"},
            {"role": "assistant", "content": "могу скинуть ссылку"},
        ]
        call_mock = AsyncMock(return_value=object())
        with (
            patch.object(coref_mod, "_call_openai_with_retry", call_mock),
            patch.object(
                coref_mod,
                "_get_output_text",
                return_value="хочу купить юси по ссылке",
            ),
        ):
            rewritten = await coref_mod.resolve_coref("давай", history)

        self.assertEqual(rewritten, "хочу купить юси по ссылке")
        self.assertNotIn("text", call_mock.await_args.kwargs)
        input_msgs = call_mock.await_args.kwargs["input"]
        self.assertIn("SHORT-TERM MEMORY CONTEXT", input_msgs[1]["content"][0]["text"])
        self.assertIn("STM (oldest -> newest)", input_msgs[1]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
