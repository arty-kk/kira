import math
import types
import unittest
from pathlib import Path
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
        self.assertEqual(reuse_counter[0], 1)


    async def test_precomputed_query_embedding_is_centered_like_runtime_embedding(self) -> None:
        model = "test-rag-centered"
        entries = [
            {"id": "left", "text": "left", "emb": [2.0, 0.0]},
            {"id": "right", "text": "right", "emb": [4.0, 0.0]},
        ]
        knowledge_proc._KB_ENTRIES[model] = entries
        knowledge_proc._KB_STATE[model] = knowledge_proc._build_state(entries)

        raw_query = [5.0, 0.0]

        hits = await knowledge_proc.get_relevant(
            "irrelevant",
            model_name=model,
            query_embedding=raw_query,
            embedding_model=model,
        )

        self.assertTrue(hits)
        self.assertEqual(hits[0][1], "right")

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
        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()


class KeywordFilterPerTagRankingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        keyword_filter._INDICES.clear()
        keyword_filter._EMB_CACHE.clear()

    async def test_find_tag_hits_uses_best_tag_per_item_and_returns_top3_texts(self) -> None:
        async def _fake_ensure_index(_model=None):
            return {
                "tag_vecs": {
                    "item-1": [[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]],
                    "item-2": [[0.95, 0.05], [-0.4, 0.9]],
                    "item-3": [[0.85, 0.15]],
                    "item-4": [[0.70, 0.30]],
                },
                "tags": {
                    "item-1": ["strong", "weak-a", "weak-b"],
                    "item-2": ["good", "weak"],
                    "item-3": ["ok"],
                    "item-4": ["lower"],
                },
                "texts": {
                    "item-1": "text-1",
                    "item-2": "text-2",
                    "item-3": "text-3",
                    "item-4": "text-4",
                },
                "model": "m",
            }

        with (
            mock.patch.object(keyword_filter, "_ensure_index", side_effect=_fake_ensure_index),
            mock.patch.object(
                keyword_filter,
                "_mmr_select_ids",
                side_effect=lambda cand_ids, _vecs, scores_by_id, top_k, lam: sorted(
                    cand_ids, key=lambda i: scores_by_id[i], reverse=True
                )[:top_k],
            ),
        ):
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=[1.0, 0.0],
                embedding_model="m",
                model="m",
                limit=3,
            )

        self.assertEqual([hid for _, hid, _ in hits], ["item-1", "item-2", "item-3"])
        self.assertEqual([txt for _, _, txt in hits], ["text-1", "text-2", "text-3"])
        self.assertGreater(hits[0][0], hits[1][0])
        self.assertGreater(hits[1][0], hits[2][0])

    def test_load_tags_index_from_npz_backward_compatible_v1(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tags_v1.npz"
            np.savez_compressed(
                p,
                E=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
                ids=np.asarray(["a", "b"], dtype=object),
                texts=np.asarray(["ta", "tb"], dtype=object),
                meta=np.asarray({"dim": 2}, dtype=object),
            )
            idx = keyword_filter._load_tags_index_from_npz(p)

        self.assertIsNotNone(idx)
        assert idx is not None
        self.assertIn("tag_vecs", idx)
        self.assertEqual(len(idx["tag_vecs"]["a"]), 1)
        self.assertEqual(len(idx["tag_vecs"]["b"]), 1)
        self.assertEqual(idx["texts"]["a"], "ta")
