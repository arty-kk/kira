import math
import types
import unittest
from unittest import mock

import numpy as np

from app.services.responder.rag import relevance
from app.services.responder.rag import knowledge_proc
from app.services.responder.rag import keyword_filter


class RagQueryEmbeddingReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        knowledge_proc._KB_ENTRIES.clear()
        knowledge_proc._KB_STATE.clear()
        knowledge_proc._EMB_CACHE.clear()
        keyword_filter._INDICES.clear()
        keyword_filter._EMB_CACHE.clear()

        self.model = "test-rag-model"
        entries = [
            {"id": "k1", "text": "alpha", "emb": [1.0, 0.0, 0.0]},
            {"id": "k2", "text": "beta", "emb": [0.0, 1.0, 0.0]},
        ]
        knowledge_proc._KB_ENTRIES[self.model] = entries
        knowledge_proc._KB_STATE[self.model] = knowledge_proc._build_state(entries)

    async def test_precomputed_query_embedding_reused_across_chain(self) -> None:
        query = "alpha question"
        calls = {"count": 0}

        async def _fake_openai_call(*, endpoint, model, input, **_kwargs):
            self.assertEqual(endpoint, "embeddings.create")
            calls["count"] += 1
            data = [types.SimpleNamespace(embedding=[1.0, 0.0, 0.0]) for _ in input]
            return types.SimpleNamespace(data=data)

        async def _fake_ensure_index(_model=None):
            return {
                "vecs": {"tag-1": [1.0, 0.0, 0.0]},
                "texts": {"tag-1": "alpha tag"},
                "model": self.model,
            }

        with (
            mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_openai_call),
            mock.patch.object(keyword_filter, "_call_openai_with_retry", side_effect=_fake_openai_call),
            mock.patch.object(keyword_filter, "_ensure_index", side_effect=_fake_ensure_index),
        ):
            qraw = await knowledge_proc._get_query_embedding(self.model, query)
            self.assertIsNotNone(qraw)
            norm = float(np.linalg.norm(qraw))
            qnorm = (qraw / norm).tolist() if norm and math.isfinite(norm) else None

            reuse_counter = [0]
            ok, hits = await relevance.is_relevant(
                query,
                model=self.model,
                threshold=0.1,
                return_hits=True,
                strict_autoreply_gate=True,
                query_embedding=qnorm,
                embedding_model=self.model,
                query_embedding_reuse_counter=reuse_counter,
            )

        self.assertTrue(ok)
        self.assertTrue(hits)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(reuse_counter[0], 2)

    async def test_backward_compatible_local_query_embedding_path(self) -> None:
        query = "alpha question"
        calls = {"count": 0}

        async def _fake_openai_call(*, endpoint, model, input, **_kwargs):
            self.assertEqual(endpoint, "embeddings.create")
            calls["count"] += 1
            data = [types.SimpleNamespace(embedding=[1.0, 0.0, 0.0]) for _ in input]
            return types.SimpleNamespace(data=data)

        async def _fake_ensure_index(_model=None):
            return {
                "vecs": {"tag-1": [1.0, 0.0, 0.0]},
                "texts": {"tag-1": "alpha tag"},
                "model": self.model,
            }

        with (
            mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_openai_call),
            mock.patch.object(keyword_filter, "_call_openai_with_retry", side_effect=_fake_openai_call),
            mock.patch.object(keyword_filter, "_ensure_index", side_effect=_fake_ensure_index),
        ):
            ok, hits = await relevance.is_relevant(
                query,
                model=self.model,
                threshold=0.1,
                return_hits=True,
                strict_autoreply_gate=True,
            )

        self.assertTrue(ok)
        self.assertTrue(hits)
        self.assertGreaterEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
