import types
from pathlib import Path
import unittest
from unittest import mock

import numpy as np
from pgvector.psycopg import HalfVector, Vector as PgVector

from app.services.responder.rag import keyword_filter
from app.services.responder.rag import relevance
from app.services.responder.rag import knowledge_proc
from app.tasks import kb


class RagTagsOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_relevance_uses_tag_hits(self):
        async def _fake_find_tag_hits(*_args, **_kwargs):
            return [(0.9, "tag-1", "text-1")]

        with mock.patch.object(relevance, "find_tag_hits", side_effect=_fake_find_tag_hits):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="m",
                threshold=0.1,
                return_hits=True,
                strict_autoreply_gate=True,
            )

        self.assertTrue(ok)
        self.assertEqual(hits[0][1], "tag-1")

    async def test_keyword_filter_uses_sql_distance_order_limit_and_output_contract(self):
        captured = {"execute_called": False}

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, query, params=None):
                captured["execute_called"] = True
                captured["query"] = query
                captured["params"] = params
                rows = [
                    (
                        "global",
                        None,
                        None,
                        "item-1",
                        "text-1",
                        None,
                        np.asarray([1.0] + [0.0] * 3071, dtype=np.float32),
                        0.99,
                        0.01,
                    ),
                    (
                        "owner",
                        42,
                        None,
                        "item-2",
                        "text-2",
                        None,
                        np.asarray([0.9, 0.1] + [0.0] * 3070, dtype=np.float32),
                        0.91,
                        0.09,
                    ),
                ]
                return _FakeResult(rows)

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=query,
                model="m",
                embedding_model="m",
                owner_id=42,
                limit=2,
            )

        sql = str(captured["query"])
        self.assertIn("<=>", sql)
        self.assertIn("ORDER BY scored.distance ASC", sql)
        self.assertIn("LIMIT", sql)
        self.assertIn("embedding_model", sql)
        self.assertIn("scope", sql)
        self.assertTrue(captured["execute_called"])
        self.assertIsInstance(captured["params"], dict)
        self.assertIn("query_vec", captured["params"])
        self.assertIsInstance(captured["params"]["query_vec"], (HalfVector, PgVector))
        self.assertEqual(len(captured["params"]["query_vec"].to_list()), 3072)
        self.assertTrue(
            all(not isinstance(item, (list, tuple, np.ndarray)) for item in captured["params"]["query_vec"].to_list())
        )
        self.assertEqual(hits, [(0.99, "global:0:0:item-1", "text-1"), (0.91, "owner:42:0:item-2", "text-2")])
        self.assertTrue(all(isinstance(hit, tuple) and len(hit) == 3 for hit in hits))
        self.assertTrue(all(isinstance(hit[0], float) and isinstance(hit[1], str) and isinstance(hit[2], str) for hit in hits))

    async def test_keyword_filter_halfvec_query_time_regression_stable_top1(self):
        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, _query, params=None):
                rows = [
                    (
                        "global",
                        None,
                        None,
                        "first",
                        "first-text",
                        None,
                        np.asarray([1.0] + [0.0] * 3071, dtype=np.float32),
                        0.95,
                        0.05,
                    ),
                    (
                        "global",
                        None,
                        None,
                        "second",
                        "second-text",
                        None,
                        np.asarray([0.95, 0.05] + [0.0] * 3070, dtype=np.float32),
                        0.88,
                        0.12,
                    ),
                ]
                return _FakeResult(rows)

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits("q", query_embedding=query, model="m", embedding_model="m", limit=1)

        self.assertEqual(hits, [(0.95, "global:0:0:first", "first-text")])
        self.assertIsInstance(hits[0], tuple)
        self.assertEqual(len(hits[0]), 3)
        self.assertIsInstance(hits[0][0], float)
        self.assertIsInstance(hits[0][1], str)
        self.assertIsInstance(hits[0][2], str)

    async def test_keyword_filter_filters_invalid_vectors_from_payload(self):
        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, _query, params=None):
                rows = [
                    ("global", None, None, "bad", "bad", None, np.asarray([1.0, 0.0], dtype=np.float32), 0.99, 0.01),
                    (
                        "global",
                        None,
                        None,
                        "ok",
                        "ok-text",
                        None,
                        np.asarray([1.0] + [0.0] * 3071, dtype=np.float32),
                        0.95,
                        0.05,
                    ),
                ]
                return _FakeResult(rows)

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits("q", query_embedding=query, model="m", embedding_model="m", limit=1)

        self.assertEqual(hits, [(0.95, "global:0:0:ok", "ok-text")])

    async def test_keyword_filter_returns_empty_when_sql_execute_raises(self):
        class _FakeSession:
            async def execute(self, _query, params=None):
                raise ValueError("boom")

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits("q", query_embedding=query, model="m", embedding_model="m", limit=1)

        self.assertEqual(hits, [])

    async def test_keyword_filter_returns_empty_on_empty_query_embedding(self):
        with mock.patch.object(keyword_filter, "session_scope") as mocked_scope:
            hits = await keyword_filter.find_tag_hits("q", query_embedding=[], model="m", embedding_model="m")

        self.assertEqual(hits, [])
        mocked_scope.assert_not_called()

    async def test_keyword_filter_returns_empty_on_2d_query_embedding(self):
        with mock.patch.object(keyword_filter, "session_scope") as mocked_scope:
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=[[1.0, 0.0], [0.0, 1.0]],
                model="m",
                embedding_model="m",
            )

        self.assertEqual(hits, [])
        mocked_scope.assert_not_called()

    async def test_keyword_filter_sql_bind_uses_non_normalized_qv_src(self):
        captured = {}

        class _FakeResult:
            def all(self):
                return []

        class _FakeSession:
            async def execute(self, _query, params=None):
                captured["params"] = params
                return _FakeResult()

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with (
            mock.patch.object(keyword_filter, "_l2_normalize", return_value=[0.5] * 3072),
            mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()),
        ):
            await keyword_filter.find_tag_hits(
                "q",
                query_embedding=query,
                model="m",
                embedding_model="m",
            )

        self.assertIn("query_vec", captured["params"])
        bound = captured["params"]["query_vec"].to_list()
        self.assertNotEqual(bound, [0.5] * 3072)
        self.assertEqual(bound, query)

    async def test_keyword_filter_preflight_rejects_ragged_embedding_without_sql(self):
        query = [[1.0] + [0.0] * 3071, [0.0] * 3070]
        with mock.patch.object(keyword_filter, "session_scope") as mocked_scope:
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=query,
                model="m",
                embedding_model="m",
            )

        self.assertEqual(hits, [])
        mocked_scope.assert_not_called()

    async def test_keyword_filter_accepts_singleton_2d_query_embedding(self):
        captured = {}

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, _query, params=None):
                captured["params"] = params
                return _FakeResult([
                    (
                        "global",
                        None,
                        None,
                        "ok",
                        "ok-text",
                        None,
                        np.asarray([1.0] + [0.0] * 3071, dtype=np.float32),
                        0.95,
                        0.05,
                    ),
                ])

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [[1.0] + [0.0] * 3071]
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits("q", query_embedding=query, model="m", embedding_model="m", limit=1)

        self.assertEqual(hits, [(0.95, "global:0:0:ok", "ok-text")])
        self.assertEqual(len(captured["params"]["query_vec"].to_list()), 3072)

    async def test_keyword_filter_returns_empty_on_query_embedding_dim_mismatch(self):
        with mock.patch.object(keyword_filter, "session_scope") as mocked_scope:
            hits = await keyword_filter.find_tag_hits("q", query_embedding=[1.0, 0.0], model="m", embedding_model="m")

        self.assertEqual(hits, [])
        mocked_scope.assert_not_called()

    async def test_keyword_filter_returns_empty_on_non_finite_query_embedding(self):
        with mock.patch.object(keyword_filter, "session_scope") as mocked_scope:
            hits_nan = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=[float("nan")] + [0.0] * 3071,
                model="m",
                embedding_model="m",
            )
            hits_inf = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=[float("inf")] + [0.0] * 3071,
                model="m",
                embedding_model="m",
            )

        self.assertEqual(hits_nan, [])
        self.assertEqual(hits_inf, [])
        mocked_scope.assert_not_called()

    async def test_get_query_embedding_singleton_nested_array(self):
        async def _fake_call(**_kwargs):
            return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[[1.0, 2.0]])])

        with mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_call):
            arr = await knowledge_proc._get_query_embedding("m", "q")

        self.assertEqual(arr.tolist(), [1.0, 2.0])

    async def test_get_query_embedding_base64(self):
        payload = np.asarray([1.0, 2.0], dtype=np.float32).tobytes()

        async def _fake_call(**_kwargs):
            return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=__import__("base64").b64encode(payload).decode("ascii"))])

        with mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_call):
            arr = await knowledge_proc._get_query_embedding("m", "q")

        self.assertEqual(arr.tolist(), [1.0, 2.0])

    async def test_get_query_embedding_empty_data_returns_none(self):
        async def _fake_call(**_kwargs):
            return types.SimpleNamespace(data=[])

        with mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_call):
            arr = await knowledge_proc._get_query_embedding("m", "q")

        self.assertIsNone(arr)

    async def test_get_query_embedding_missing_embedding_returns_none(self):
        async def _fake_call(**_kwargs):
            return types.SimpleNamespace(data=[types.SimpleNamespace()])

        with mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_call):
            arr = await knowledge_proc._get_query_embedding("m", "q")

        self.assertIsNone(arr)


    async def test_keyword_filter_small_model_is_forced_to_large_and_rejects_1536_query(self):
        captured = {}

        class _FakeResult:
            def all(self):
                return [(
                    "global", None, None, "item-small", "text-small", None,
                    np.asarray([1.0] + [0.0] * 1535, dtype=np.float32), 0.97, 0.03,
                )]

        class _FakeSession:
            async def execute(self, query, params=None):
                captured["query"] = query
                captured["params"] = params
                return _FakeResult()

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 1535
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=query,
                model="text-embedding-3-small",
                embedding_model="text-embedding-3-small",
                limit=1,
            )

        self.assertEqual(hits, [])
        self.assertNotIn("params", captured)

    async def test_keyword_filter_vector_bind_and_distance_sql_for_large_dim(self):
        captured = {}

        class _FakeResult:
            def all(self):
                return [(
                    "global", None, None, "item-large", "text-large", None,
                    np.asarray([1.0] + [0.0] * 3071, dtype=np.float32), 0.98, 0.02,
                )]

        class _FakeSession:
            async def execute(self, query, params=None):
                captured["query"] = query
                captured["params"] = params
                return _FakeResult()

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits(
                "q",
                query_embedding=query,
                model="text-embedding-3-large",
                embedding_model="text-embedding-3-large",
                limit=1,
            )

        self.assertEqual(hits, [(0.98, "global:0:0:item-large", "text-large")])
        self.assertIsInstance(captured["params"]["query_vec"], HalfVector)
        self.assertEqual(len(captured["params"]["query_vec"].to_list()), 3072)
        sql = str(captured["query"])
        self.assertIn("<=>", sql)
        self.assertNotIn("CAST(rag_tag_vectors.embedding AS halfvec(3072))", sql)
        self.assertNotIn("CAST(:query_vec AS halfvec(3072))", sql)


    def test_embedding_param_consistent_vector_type_for_small_and_large_dims(self):
        small = [0.1] * 1536
        large = [0.1] * 3072

        kb_small = kb._embedding_param(small, expected_dim=1536)
        kb_large = kb._embedding_param(large, expected_dim=3072)
        qf_small = keyword_filter._embedding_param(small, expected_dim=1536)
        qf_large = keyword_filter._embedding_param(large, expected_dim=3072)

        self.assertIsInstance(kb_small, HalfVector)
        self.assertIsInstance(kb_large, HalfVector)
        self.assertIsInstance(qf_small, HalfVector)
        self.assertIsInstance(qf_large, HalfVector)
        self.assertEqual(len(kb_small.to_list()), 1536)
        self.assertEqual(len(kb_large.to_list()), 3072)

    def test_build_vector_distance_expr_uses_column_type_bind(self):
        distance_raw, _distance_sel = keyword_filter._build_vector_distance_expr(dim=1536)
        sql = str(distance_raw)

        self.assertIn('rag_tag_vectors.embedding <=>', sql)


    def test_rag_vector_contour_schema_query_and_bind_are_aligned(self):
        schema_text = Path("alembic/versions/0001_initial_schema.py").read_text()

        self.assertIn("embedding halfvec_cosine_ops", schema_text)
        self.assertNotIn("vector_cosine_ops", schema_text)

        self.assertEqual(type(keyword_filter.RagTagVector.embedding.type), type(kb.RagTagVector.embedding.type))
        self.assertEqual(keyword_filter.RagTagVector.embedding.type.__class__.__name__, "HalfVector")

        vec_1536 = [0.1] * 1536
        bind_write = kb._embedding_param(vec_1536, expected_dim=1536, model="text-embedding-3-small")
        bind_query = keyword_filter._embedding_param(vec_1536, expected_dim=1536, model="text-embedding-3-small")

        self.assertIsInstance(bind_write, HalfVector)
        self.assertIsInstance(bind_query, HalfVector)
        self.assertEqual(bind_write.to_list(), bind_query.to_list())

        distance_raw, _distance_sel = keyword_filter._build_vector_distance_expr(dim=3072)
        distance_sql = str(distance_raw)
        self.assertIn("rag_tag_vectors.embedding <=>", distance_sql)


if __name__ == "__main__":
    unittest.main()
