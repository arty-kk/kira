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

    async def test_keyword_filter_returns_best_texts_by_tag_similarity(self):
        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, _query):
                rows = [
                    ("global", None, "item-1", "text-1", [1.0] + [0.0] * 3071, 0.99),
                    ("global", None, "item-2", "text-2", [0.9, 0.1] + [0.0] * 3070, 0.91),
                ]
                return _FakeResult(rows)

        class _FakeScope:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        query = [1.0] + [0.0] * 3071
        with mock.patch.object(keyword_filter, "session_scope", return_value=_FakeScope()):
            hits = await keyword_filter.find_tag_hits("q", query_embedding=query, model="m", embedding_model="m", limit=2)

        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0][1], "item-1")


    async def test_keyword_filter_accepts_numpy_embedding_from_db(self):
        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _FakeSession:
            async def execute(self, _query):
                rows = [
                    ("global", None, "item-1", "text-1", np.asarray([1.0] + [0.0] * 3071, dtype=np.float32), 0.99),
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

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "item-1")

    async def test_get_query_embedding_base64(self):
        payload = np.asarray([1.0, 2.0], dtype=np.float32).tobytes()

        async def _fake_call(**_kwargs):
            return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=__import__("base64").b64encode(payload).decode("ascii"))])

        with mock.patch.object(knowledge_proc, "_call_openai_with_retry", side_effect=_fake_call):
            arr = await knowledge_proc._get_query_embedding("m", "q")

        self.assertEqual(arr.tolist(), [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
