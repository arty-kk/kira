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
                "E": np.asarray(
                    [
                        [1.0, 0.0],
                        [0.0, 1.0],
                        [0.95, 0.05],
                        [0.1, 0.9],
                        [0.85, 0.15],
                        [0.70, 0.30],
                    ],
                    dtype=np.float32,
                ),
                "row_to_eid": ["item-1", "item-1", "item-2", "item-2", "item-3", "item-4"],
                "row_to_tag": ["strong", "weak-a", "good", "weak", "ok", "lower"],
                "row_to_text": ["text-1", "text-1", "text-2", "text-2", "text-3", "text-4"],
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

        self.assertTrue(all(isinstance(x, tuple) and len(x) == 3 for x in hits))
        self.assertEqual([hid for _, hid, _ in hits], ["item-1", "item-2", "item-3"])
        self.assertEqual([txt for _, _, txt in hits], ["text-1", "text-2", "text-3"])
        self.assertGreater(hits[0][0], hits[1][0])
        self.assertGreater(hits[1][0], hits[2][0])

    async def test_find_tag_hits_limits_candidates_before_mmr(self) -> None:
        top_n = keyword_filter.MMR_CANDIDATES_TOP_N
        rows = []
        row_to_eid = []
        row_to_tag = []
        row_to_text = []
        for i in range(top_n + 5):
            rows.append([1.0 - (i * 0.01), 0.0])
            row_to_eid.append(f"item-{i}")
            row_to_tag.append(f"tag-{i}")
            row_to_text.append(f"text-{i}")

        async def _fake_ensure_index(_model=None):
            return {
                "E": np.asarray(rows, dtype=np.float32),
                "row_to_eid": row_to_eid,
                "row_to_tag": row_to_tag,
                "row_to_text": row_to_text,
                "model": "m",
            }

        captured = {}

        def _fake_mmr(cand_ids, _vecs, scores_by_id, top_k, lam):
            captured["cand_count"] = len(cand_ids)
            return sorted(cand_ids, key=lambda i: scores_by_id[i], reverse=True)[:top_k]

        with (
            mock.patch.object(keyword_filter, "_ensure_index", side_effect=_fake_ensure_index),
            mock.patch.object(keyword_filter, "_mmr_select_ids", side_effect=_fake_mmr),
        ):
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=[1.0, 0.0],
                embedding_model="m",
                model="m",
                limit=5,
            )

        self.assertEqual(captured["cand_count"], top_n)
        self.assertEqual(len(hits), 5)

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
        self.assertIn("E", idx)
        self.assertEqual(idx["E"].shape, (2, 2))
        self.assertEqual(idx["row_to_eid"], ["a", "b"])
        self.assertEqual(idx["row_to_text"], ["ta", "tb"])

    def test_load_tags_index_from_npz_backward_compatible_vecs_single_vector(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tags_vecs.npz"
            np.savez_compressed(
                p,
                vecs=np.asarray({"a": [1.0, 0.0], "b": [0.0, 1.0]}, dtype=object),
                texts=np.asarray({"a": "ta", "b": "tb"}, dtype=object),
            )
            idx = keyword_filter._load_tags_index_from_npz(p)

        self.assertIsNotNone(idx)
        assert idx is not None
        self.assertEqual(idx["row_to_eid"], ["a", "b"])
        self.assertEqual(idx["E"].shape, (2, 2))

    def test_load_tags_index_from_npz_backward_compatible_vecs_ndarray_rows(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tags_vecs_nd.npz"
            np.savez_compressed(
                p,
                vecs=np.asarray({"a": np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)}, dtype=object),
                texts=np.asarray({"a": "ta"}, dtype=object),
                tags=np.asarray({"a": ["first", "second"]}, dtype=object),
            )
            idx = keyword_filter._load_tags_index_from_npz(p)

        self.assertIsNotNone(idx)
        assert idx is not None
        self.assertEqual(idx["row_to_eid"], ["a", "a"])
        self.assertEqual(idx["row_to_tag"], ["first", "second"])
        self.assertEqual(idx["E"].shape, (2, 2))

    def test_load_tags_index_from_npz_backward_compatible_tag_vecs_single_vector(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tags_tag_vecs_single.npz"
            np.savez_compressed(
                p,
                tag_vecs=np.asarray({"a": np.asarray([1.0, 0.0], dtype=np.float32)}, dtype=object),
                texts=np.asarray({"a": "ta"}, dtype=object),
                tags=np.asarray({"a": ["first"]}, dtype=object),
            )
            idx = keyword_filter._load_tags_index_from_npz(p)

        self.assertIsNotNone(idx)
        assert idx is not None
        self.assertEqual(idx["row_to_eid"], ["a"])
        self.assertEqual(idx["row_to_tag"], ["first"])
        self.assertEqual(idx["E"].shape, (1, 2))

    async def test_find_tag_hits_handles_embedding_dim_mismatch(self) -> None:
        async def _fake_ensure_index(_model=None):
            return {
                "E": np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
                "row_to_eid": ["item-1", "item-2"],
                "row_to_tag": ["t1", "t2"],
                "row_to_text": ["text-1", "text-2"],
                "model": "m",
            }

        with mock.patch.object(keyword_filter, "_ensure_index", side_effect=_fake_ensure_index):
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=[1000.0, 0.0, 1.0],
                embedding_model="m",
                model="m",
                limit=2,
            )

        self.assertEqual([hid for _, hid, _ in hits], ["item-1"])
