import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.responder import core


class _Persona:
    def __init__(self):
        self.state = {}

    async def ready(self, timeout=5.0):
        return True

    async def process_interaction(self, *_args, **_kwargs):
        return None

    async def summary(self):
        return "summary"

    async def style_guidelines(self, *_args, **_kwargs):
        return "guidelines"

    async def style_modifiers(self):
        return {}


class _FakeSessionScope:
    async def __aenter__(self):
        class _DB:
            async def get(self, *_args, **_kwargs):
                return SimpleNamespace(gender=None, free_requests=0, paid_requests=0)

        return _DB()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ResponderStmPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_respond_to_user_adds_separate_stm_block_and_keeps_history_shape(self):
        redis_stub = type(
            "RedisStub",
            (),
            {
                "hgetall": AsyncMock(return_value={}),
                "get": AsyncMock(return_value=None),
                "set": AsyncMock(return_value=True),
                "delete": AsyncMock(return_value=1),
            },
        )()

        history = [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "И тебе привет!"},
        ]

        llm_call_mock = AsyncMock(return_value=SimpleNamespace())

        with patch.object(core, "session_scope", return_value=_FakeSessionScope()), \
             patch.object(core, "get_redis", return_value=redis_stub), \
             patch.object(core, "get_persona", AsyncMock(return_value=_Persona())), \
             patch.object(core, "get_cached_gender", AsyncMock(return_value=None)), \
             patch.object(core, "build_system_prompt", AsyncMock(return_value="sys")), \
             patch.object(core, "load_context", AsyncMock(return_value=history.copy())), \
             patch.object(core, "needs_coref", AsyncMock(return_value=False)), \
             patch.object(
                 core,
                 "_compute_on_topic_relevance",
                 AsyncMock(
                     return_value=(
                         False,
                         None,
                         core.RagQueryContext(query="Новый вопрос", rag_query_source="raw"),
                     )
                 ),
             ), \
             patch.object(core, "get_ltm_slices", AsyncMock(return_value=[])), \
             patch.object(core, "get_ltm_text", AsyncMock(return_value="ltm raw")), \
             patch.object(core, "get_all_mtm_texts", AsyncMock(return_value=["mtm raw"])), \
             patch.object(core, "summarize_mtm_topic", AsyncMock(return_value="topic")), \
             patch.object(core, "compose_mtm_snippet", AsyncMock(return_value="mtm snippet")), \
             patch.object(core, "select_snippets_via_nano", AsyncMock(return_value="ltm snippet")), \
             patch.object(core, "get_group_stm_tail", AsyncMock(return_value=[])), \
             patch.object(core, "record_context", AsyncMock(return_value=None)), \
             patch.object(core, "record_assistant_reply", AsyncMock(return_value=None)), \
             patch.object(core, "record_latency", AsyncMock(return_value=None)), \
             patch.object(core, "_call_openai_with_retry", llm_call_mock), \
             patch.object(core, "_get_output_text", return_value="ok"):
            out = await core.respond_to_user(
                text="Новый вопрос",
                chat_id=101,
                user_id=202,
                skip_user_push=True,
                skip_assistant_push=True,
                skip_persona_interaction=True,
            )

        self.assertEqual(out, "ok")
        messages = llm_call_mock.await_args.kwargs["input"]

        system_text = messages[0]["content"][0]["text"]
        self.assertIn("SHORT-TERM MEMORY CONTEXT", system_text)
        self.assertIn(
            "STM is your (Assistant) current conversation history with the user (User).",
            system_text,
        )
        self.assertIn("MID-TERM MEMORY SNIPPETS", system_text)
        self.assertIn("LONG-TERM MEMORY SNIPPETS", system_text)
        self.assertLess(system_text.index("SHORT-TERM MEMORY CONTEXT"), system_text.index("MID-TERM MEMORY SNIPPETS"))

        history_messages = messages[1:-1]
        for msg in history_messages:
            self.assertIn(msg["role"], ("user", "assistant"))
            self.assertEqual(len(msg["content"]), 1)
            self.assertEqual(
                msg["content"][0]["type"],
                "output_text" if msg["role"] == "assistant" else "input_text",
            )

        current_user = messages[-1]
        self.assertEqual(current_user["role"], "user")
        self.assertEqual(current_user["content"][0]["type"], "input_text")
        self.assertEqual(current_user["content"][0]["text"], "Новый вопрос")


if __name__ == "__main__":
    unittest.main()
