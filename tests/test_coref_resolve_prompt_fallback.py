import unittest
from unittest.mock import AsyncMock, patch

import importlib

coref_mod = importlib.import_module("app.services.responder.coref.resolve_coref")


class CorefResolvePromptFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_coref_falls_back_to_prompt_rewrite_when_no_links(self) -> None:
        history = [
            {"role": "user", "content": "хочу купить юси"},
            {"role": "assistant", "content": "могу скинуть ссылку"},
        ]
        with (
            patch.object(coref_mod, "_call_openai_with_retry", AsyncMock(return_value=object())),
            patch.object(
                coref_mod,
                "_get_output_text",
                side_effect=['{"links":[]}', '{"rewritten":"хочу купить юси по ссылке"}'],
            ),
        ):
            rewritten = await coref_mod.resolve_coref("давай", history)

        self.assertEqual(rewritten, "хочу купить юси по ссылке")


if __name__ == "__main__":
    unittest.main()
