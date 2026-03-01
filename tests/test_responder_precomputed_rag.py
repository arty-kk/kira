import unittest
from unittest.mock import AsyncMock, patch

from app.services.responder import core


class ResponderPrecomputedRagTests(unittest.IsolatedAsyncioTestCase):
    async def test_precomputed_hits_and_embedding_skip_duplicate_precheck(self):
        precomputed_hits = [(0.91, "id-1", "chunk")]
        precomputed_embedding = [0.1] + [0.0] * 3071

        with patch.object(core, "is_relevant", AsyncMock(return_value=(False, None))) as is_relevant_mock, \
             patch.object(core, "_get_query_embedding", AsyncMock(return_value=[0.9, 0.8])) as get_embedding_mock:
            on_topic_flag, on_topic_hits, rag_ctx = await core._compute_on_topic_relevance(
                chat_id=100,
                query_to_model="topic",
                trigger="check_on_topic",
                persona_owner_id=100,
                knowledge_owner_id=100,
                knowledge_kb_id=None,
                precomputed_rag_hits=precomputed_hits,
                query_embedding=precomputed_embedding,
                embedding_model="text-embedding-3-large",
                rag_precheck_source="queue_worker_tag_precheck",
                rag_query_source="raw",
            )

        self.assertTrue(on_topic_flag)
        self.assertEqual(on_topic_hits, precomputed_hits)
        self.assertEqual(len(rag_ctx.query_embedding or []), 3072)
        self.assertAlmostEqual((rag_ctx.query_embedding or [0.0])[0], 0.1, places=6)
        self.assertEqual(rag_ctx.embedding_model, "text-embedding-3-large")
        self.assertEqual(rag_ctx.rag_query_source, "raw")
        self.assertEqual(rag_ctx.query_embedding_source, "queue_worker_tag_precheck")
        get_embedding_mock.assert_not_awaited()
        is_relevant_mock.assert_not_awaited()

    async def test_raw_rag_query_is_passed_to_embedding_and_relevance(self):
        with patch.object(core, "_get_query_embedding", AsyncMock(return_value=[0.2, 0.1])) as get_embedding_mock, \
             patch.object(core, "is_relevant", AsyncMock(return_value=(True, [(0.9, "id", "txt")]))) as is_relevant_mock:
            ok, _hits, rag_ctx = await core._compute_on_topic_relevance(
                chat_id=100,
                query_to_model="raw rag query",
                trigger="mention",
                persona_owner_id=100,
                knowledge_owner_id=100,
                knowledge_kb_id=None,
                precomputed_rag_hits=None,
                query_embedding=None,
                embedding_model=None,
                rag_precheck_source=None,
                rag_query_source="raw",
            )

        self.assertTrue(ok)
        get_embedding_mock.assert_awaited_once()
        self.assertEqual(get_embedding_mock.await_args.args[1], "raw rag query")
        is_relevant_mock.assert_awaited_once()
        self.assertEqual(is_relevant_mock.await_args.args[0], "raw rag query")
        self.assertEqual(rag_ctx.rag_query_source, "raw")

    async def test_autoreply_runs_strict_trigger_then_generation_relevance(self):
        is_relevant_mock = AsyncMock(side_effect=[(True, [(0.81, "id-1", "strict")]), (True, [(0.41, "id-2", "gen")])])
        with patch.object(core, "is_relevant", is_relevant_mock),              patch.object(core, "_get_query_embedding", AsyncMock(return_value=[0.2, 0.1])):
            ok, hits, _ = await core._compute_on_topic_relevance(
                chat_id=100,
                query_to_model="autotrigger",
                trigger="check_on_topic",
                persona_owner_id=100,
                knowledge_owner_id=100,
                knowledge_kb_id=None,
                precomputed_rag_hits=None,
                query_embedding=None,
                embedding_model=None,
                rag_precheck_source=None,
                rag_query_source="raw",
            )

        self.assertTrue(ok)
        self.assertEqual(hits, [(0.41, "id-2", "gen")])
        self.assertEqual(is_relevant_mock.await_count, 2)
        self.assertTrue(is_relevant_mock.await_args_list[0].kwargs.get("strict_autoreply_gate"))
        self.assertFalse(is_relevant_mock.await_args_list[1].kwargs.get("strict_autoreply_gate"))
        self.assertEqual(
            is_relevant_mock.await_args_list[1].kwargs.get("threshold"),
            float(getattr(core.settings, "RELEVANCE_THRESHOLD", 0.28) or 0.28),
        )


if __name__ == "__main__":
    unittest.main()

class ResponderRawRagRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_respond_to_user_uses_rewritten_query_for_rag_when_coref_rewrites(self):
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

        compute_mock = AsyncMock(return_value=(False, None, core.RagQueryContext(query="What about it?", rag_query_source="raw")))

        needs_coref_mock = AsyncMock(return_value=True)

        with patch.object(core, "get_redis", return_value=redis_stub), \
             patch.object(core, "get_persona", AsyncMock(return_value=_Persona())), \
             patch.object(core, "get_cached_gender", AsyncMock(return_value=None)), \
             patch.object(core, "build_system_prompt", AsyncMock(return_value="sys")), \
             patch.object(core, "load_context", AsyncMock(return_value=[])), \
             patch.object(core, "record_context", AsyncMock(return_value=None)), \
             patch.object(core, "needs_coref", needs_coref_mock), \
             patch.object(core, "resolve_coref", AsyncMock(return_value="rewritten query")), \
             patch.object(core, "_compute_on_topic_relevance", compute_mock), \
             patch.object(core, "get_ltm_slices", AsyncMock(return_value=[])), \
             patch.object(core, "get_ltm_text", AsyncMock(return_value="")), \
             patch.object(core, "get_all_mtm_texts", AsyncMock(return_value=[])):

            out = await core.respond_to_user(
                text="What about it?",
                chat_id=1,
                user_id=2,
                trigger="api",
                enforce_on_topic=True,
                skip_user_push=True,
                skip_assistant_push=True,
                skip_persona_interaction=True,
            )

        self.assertEqual(out, "")
        compute_mock.assert_awaited_once()
        needs_coref_mock.assert_awaited_once()
        self.assertIn("history", needs_coref_mock.await_args.kwargs)
        self.assertEqual(compute_mock.await_args.kwargs.get("query_to_model"), "rewritten query")
        self.assertEqual(compute_mock.await_args.kwargs.get("rag_query_source"), "rewritten")

    async def test_respond_to_user_uses_rewritten_query_for_rag_when_raw_query_empty(self):
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

        compute_mock = AsyncMock(return_value=(False, None, core.RagQueryContext(query="rewritten query", rag_query_source="rewritten")))

        with patch.object(core, "get_redis", return_value=redis_stub), \
             patch.object(core, "get_persona", AsyncMock(return_value=_Persona())), \
             patch.object(core, "get_cached_gender", AsyncMock(return_value=None)), \
             patch.object(core, "build_system_prompt", AsyncMock(return_value="sys")), \
             patch.object(core, "load_context", AsyncMock(return_value=[])), \
             patch.object(core, "record_context", AsyncMock(return_value=None)), \
             patch.object(core, "_strip_bot_mention_prefix", return_value=""), \
             patch.object(core, "needs_coref", AsyncMock(return_value=True)), \
             patch.object(core, "resolve_coref", AsyncMock(return_value="rewritten query")), \
             patch.object(core, "_compute_on_topic_relevance", compute_mock), \
             patch.object(core, "get_ltm_slices", AsyncMock(return_value=[])), \
             patch.object(core, "get_ltm_text", AsyncMock(return_value="")), \
             patch.object(core, "get_all_mtm_texts", AsyncMock(return_value=[])):

            out = await core.respond_to_user(
                text="@bot mention",
                chat_id=1,
                user_id=2,
                trigger="api",
                enforce_on_topic=True,
                skip_user_push=True,
                skip_assistant_push=True,
                skip_persona_interaction=True,
            )

        self.assertEqual(out, "")
        compute_mock.assert_awaited_once()
        self.assertEqual(compute_mock.await_args.kwargs.get("query_to_model"), "rewritten query")
        self.assertEqual(compute_mock.await_args.kwargs.get("rag_query_source"), "rewritten")


    async def test_respond_to_user_does_not_reuse_raw_embedding_or_hits_when_rewritten_query_used(self):
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

        compute_mock = AsyncMock(return_value=(False, None, core.RagQueryContext(query="rewritten query", rag_query_source="rewritten")))

        needs_coref_mock = AsyncMock(return_value=True)

        with patch.object(core, "get_redis", return_value=redis_stub), \
             patch.object(core, "get_persona", AsyncMock(return_value=_Persona())), \
             patch.object(core, "get_cached_gender", AsyncMock(return_value=None)), \
             patch.object(core, "build_system_prompt", AsyncMock(return_value="sys")), \
             patch.object(core, "load_context", AsyncMock(return_value=[])), \
             patch.object(core, "record_context", AsyncMock(return_value=None)), \
             patch.object(core, "needs_coref", needs_coref_mock), \
             patch.object(core, "resolve_coref", AsyncMock(return_value="rewritten query")), \
             patch.object(core, "_compute_on_topic_relevance", compute_mock), \
             patch.object(core, "get_ltm_slices", AsyncMock(return_value=[])), \
             patch.object(core, "get_ltm_text", AsyncMock(return_value="")), \
             patch.object(core, "get_all_mtm_texts", AsyncMock(return_value=[])):

            out = await core.respond_to_user(
                text="What about it?",
                chat_id=1,
                user_id=2,
                trigger="api",
                enforce_on_topic=True,
                skip_user_push=True,
                skip_assistant_push=True,
                skip_persona_interaction=True,
                query_embedding=[0.1] + [0.0] * 3071,
                precomputed_rag_hits=[(0.93, "id-1", "chunk")],
                rag_precheck_source="queue_worker_tag_precheck",
            )

        self.assertEqual(out, "")
        compute_mock.assert_awaited_once()
        self.assertIsNone(compute_mock.await_args.kwargs.get("query_embedding"))
        self.assertIsNone(compute_mock.await_args.kwargs.get("precomputed_rag_hits"))
        self.assertIsNone(compute_mock.await_args.kwargs.get("rag_precheck_source"))
        self.assertEqual(compute_mock.await_args.kwargs.get("rag_query_source"), "rewritten")

class ResponderRequestEmbeddingContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_embedding_context_reused_by_persona_and_rag(self):
        class _Persona:
            def __init__(self):
                self.state = {}
                self.style_calls = []

            async def ready(self, timeout=5.0):
                return True

            async def process_interaction(self, *_args, **_kwargs):
                return None

            async def summary(self):
                return "summary"

            async def style_guidelines(self, *_args, **kwargs):
                self.style_calls.append(kwargs)
                return ["guideline"]

            async def style_modifiers(self):
                return {}

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

        persona = _Persona()
        compute_mock = AsyncMock(return_value=(False, None, core.RagQueryContext(query="topic", rag_query_source="raw")))

        with patch.object(core, "get_redis", return_value=redis_stub), \
             patch.object(core, "get_persona", AsyncMock(return_value=persona)), \
             patch.object(core, "get_cached_gender", AsyncMock(return_value=None)), \
             patch.object(core, "build_system_prompt", AsyncMock(return_value="sys")), \
             patch.object(core, "load_context", AsyncMock(return_value=[])), \
             patch.object(core, "record_context", AsyncMock(return_value=None)), \
             patch.object(core, "needs_coref", AsyncMock(return_value=False)), \
             patch.object(core, "_compute_on_topic_relevance", compute_mock), \
             patch.object(core, "get_ltm_slices", AsyncMock(return_value=[])), \
             patch.object(core, "get_ltm_text", AsyncMock(return_value="")), \
             patch.object(core, "get_all_mtm_texts", AsyncMock(return_value=[])), \
             patch.object(core.logger, "info") as logger_info:

            out = await core.respond_to_user(
                text="topic",
                chat_id=1,
                user_id=2,
                trigger="api",
                enforce_on_topic=True,
                skip_user_push=True,
                skip_assistant_push=True,
                skip_persona_interaction=True,
                query_embedding=[0.1] + [0.0] * 3071,
                embedding_model="text-embedding-3-large",
            )

        self.assertEqual(out, "")
        self.assertEqual(len(persona.style_calls[0]["precomputed_embedding"]), 3072)
        self.assertAlmostEqual(persona.style_calls[0]["precomputed_embedding"][0], 0.1, places=6)
        self.assertAlmostEqual(persona.style_calls[0]["precomputed_embedding"][1], 0.0, places=6)
        self.assertEqual(len(compute_mock.await_args.kwargs["query_embedding"]), 3072)

        rag_log = None
        for call in logger_info.call_args_list:
            if call.args and call.args[0] == "RAG query embedding context":
                rag_log = call
                break
        self.assertIsNotNone(rag_log)
        self.assertEqual(rag_log.kwargs["extra"]["embedding_source"], "reused")
