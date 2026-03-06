import unittest

from app.clients import openai_client
from app.prompts_base import (
    CONTEXT_RERANK_INSTRUCTIONS_STATIC_TEMPLATE,
    CONTEXT_SELECT_SNIPPETS_DEFAULT_INSTRUCTIONS_STATIC,
    CONTEXT_SELECT_SNIPPETS_MTM_INSTRUCTIONS_STATIC,
    CONTEXT_TOPIC_SUMMARY_INSTRUCTIONS_STATIC,
    GROUP_PING_INSTRUCTIONS_STATIC,
    GROUP_PING_PROMPT_WITH_CTX_TEMPLATE,
    WELCOME_INSTRUCTIONS_STATIC,
    WELCOME_PRIVATE_INSTRUCTIONS_STATIC,
    context_rerank_user_payload,
    context_select_snippets_user_payload,
    context_topic_summary_user_payload,
)
from app.services.responder.coref.needs_coref import _build_user_block


class PromptStaticBlocksTests(unittest.TestCase):
    def test_group_ping_static_block_is_template_free_and_runtime_separate(self) -> None:
        self.assertNotIn("{", GROUP_PING_INSTRUCTIONS_STATIC)
        runtime = GROUP_PING_PROMPT_WITH_CTX_TEMPLATE.format(mem_ctx="ctx", arm_hint="hint")
        self.assertIn("ctx", runtime)
        self.assertIn("hint", runtime)

    def test_welcome_static_blocks_are_template_free(self) -> None:
        self.assertNotIn("{", WELCOME_INSTRUCTIONS_STATIC)
        self.assertNotIn("{", WELCOME_PRIVATE_INSTRUCTIONS_STATIC)

    def test_context_select_static_and_dynamic_are_split(self) -> None:
        instructions = CONTEXT_RERANK_INSTRUCTIONS_STATIC_TEMPLATE.format(k=3)
        user_payload = context_rerank_user_payload("q", "[1] xxx")
        self.assertNotIn("Query:\nq", instructions)
        self.assertIn("Query:\nq", user_payload)

        mtm_payload = context_select_snippets_user_payload("src", "q", ["a", "b"], 99, mtm=True)
        default_payload = context_select_snippets_user_payload("src", "q", ["a", "b"], 99, mtm=False)
        self.assertIn("Max output budget", mtm_payload)
        self.assertIn("Max output budget", default_payload)
        self.assertNotIn("source", CONTEXT_SELECT_SNIPPETS_MTM_INSTRUCTIONS_STATIC.lower())
        self.assertIn("merged", CONTEXT_SELECT_SNIPPETS_DEFAULT_INSTRUCTIONS_STATIC)

        topic_payload = context_topic_summary_user_payload("[user] hi")
        self.assertIn("Messages:\n[user] hi", topic_payload)
        self.assertNotIn("[user] hi", CONTEXT_TOPIC_SUMMARY_INSTRUCTIONS_STATIC)

    def test_coref_user_block_is_explicit_message(self) -> None:
        block = _build_user_block("hello", history=[{"role": "user", "content": "prev"}])
        self.assertEqual(block.get("role"), "user")
        content = block.get("content")
        self.assertIsInstance(content, list)
        self.assertEqual(content[0].get("type"), "input_text")

    def test_prepare_payload_keeps_first_instruction_block_stable(self) -> None:
        p1 = openai_client._prepare_responses_payload(
            prompt_profile="pp",
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": GROUP_PING_INSTRUCTIONS_STATIC}]},
                {"role": "user", "content": [{"type": "input_text", "text": "ctx=1"}]},
            ],
        )
        p2 = openai_client._prepare_responses_payload(
            prompt_profile="pp",
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": GROUP_PING_INSTRUCTIONS_STATIC}]},
                {"role": "user", "content": [{"type": "input_text", "text": "ctx=2"}]},
            ],
        )
        self.assertEqual(p1["input"][0], p2["input"][0])


if __name__ == "__main__":
    unittest.main()
