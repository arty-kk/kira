import types
import unittest
from unittest import mock

import numpy as np

from app.services.responder.rag import keyword_filter
from app.services.responder.rag import relevance
from app.services.responder.rag import knowledge_proc


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
        captured = {}

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, query, params=None):
                captured["query"] = query
                captured["params"] = params
                rows = [
                    (
                        "global",
                        None,
                        "item-1",
                        "text-1",
                        np.asarray([1.0] + [0.0] * 3071, dtype=np.float32),
                        0.99,
                        0.01,
                    ),
                    (
                        "owner",
                        42,
                        "item-2",
                        "text-2",
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
        self.assertIn("CAST", sql)
        self.assertIn("HALFVEC", sql)
        self.assertIn("ORDER BY distance ASC", sql)
        self.assertIn("LIMIT", sql)
        self.assertIn("embedding_model", sql)
        self.assertIn("scope", sql)
        self.assertIsInstance(captured["params"], dict)
        self.assertIn("query_vec", captured["params"])
        self.assertIsInstance(captured["params"]["query_vec"], list)
        self.assertEqual(len(captured["params"]["query_vec"]), 3072)
        self.assertTrue(all(isinstance(x, float) for x in captured["params"]["query_vec"]))
        self.assertEqual(hits, [(0.99, "item-1", "text-1"), (0.91, "42:item-2", "text-2")])
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
                        "first",
                        "first-text",
                        np.asarray([1.0] + [0.0] * 3071, dtype=np.float32),
                        0.95,
                        0.05,
                    ),
                    (
                        "global",
                        None,
                        "second",
                        "second-text",
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

        self.assertEqual(hits, [(0.95, "first", "first-text")])
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
                    ("global", None, "bad", "bad", np.asarray([1.0, 0.0], dtype=np.float32), 0.99, 0.01),
                    (
                        "global",
                        None,
                        "ok",
                        "ok-text",
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

        self.assertEqual(hits, [(0.95, "ok", "ok-text")])

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
                        "ok",
                        "ok-text",
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

        self.assertEqual(hits, [(0.95, "ok", "ok-text")])
        self.assertEqual(len(captured["params"]["query_vec"]), 3072)

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


if __name__ == "__main__":
    unittest.main()
