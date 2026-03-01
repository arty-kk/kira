import unittest
import unittest.mock

from app.services.responder.rag import relevance


class RelevanceKnowledgeOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_relevant_uses_only_tag_hits_and_owner_scope(self) -> None:
        captured_owner_ids = []

        async def _fake_find_tag_hits(*_args, **kwargs):
            captured_owner_ids.append(kwargs.get("owner_id"))
            return [(0.99, "i1", "t1")]

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.1,
                return_hits=True,
                persona_owner_id=101,
                knowledge_owner_id=202,
            )

        self.assertTrue(ok)
        self.assertEqual(captured_owner_ids, [202])
        self.assertEqual(hits, [(0.99, "i1", "t1")])

    async def test_is_relevant_owner_scope_none_for_missing_or_invalid_owner(self) -> None:
        invalid_values = [None, 0, -1, "abc"]

        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                captured_owner_ids = []

                async def _fake_find_tag_hits(*_args, **kwargs):
                    captured_owner_ids.append(kwargs.get("owner_id"))
                    return [(0.95, "s", "system")]

                with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
                    ok, hits = await relevance.is_relevant(
                        "hello",
                        model="test-model",
                        threshold=0.1,
                        return_hits=True,
                        knowledge_owner_id=invalid_value,
                    )

                self.assertTrue(ok)
                self.assertEqual(captured_owner_ids, [None])
                self.assertEqual(hits, [(0.95, "s", "system")])

    async def test_is_relevant_does_not_auto_accept_weak_tag_hits_strict(self) -> None:
        async def _fake_find_tag_hits(*_args, **_kwargs):
            return [(0.10, "tag", "tag-hit")]

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, _hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.50,
                return_hits=False,
                knowledge_owner_id=None,
                strict_autoreply_gate=True,
            )

        self.assertFalse(ok)

    async def test_is_relevant_accepts_strong_tag_hits_strict(self) -> None:
        async def _fake_find_tag_hits(*_args, **_kwargs):
            return [(0.95, "tag", "tag-hit")]

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.50,
                return_hits=True,
                knowledge_owner_id=None,
                strict_autoreply_gate=True,
            )

        self.assertTrue(ok)
        self.assertEqual(hits, [(0.95, "tag", "tag-hit")])

    async def test_is_relevant_non_strict_keeps_keyword_auto_pass(self) -> None:
        async def _fake_find_tag_hits(*_args, **_kwargs):
            return [(0.10, "tag", "tag-hit")]

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.50,
                return_hits=True,
                knowledge_owner_id=None,
            )

        self.assertTrue(ok)
        self.assertEqual(hits, [(0.10, "tag", "tag-hit")])


    async def test_is_relevant_uses_trigger_threshold_for_autoreply(self) -> None:
        captured = {}

        async def _fake_find_tag_hits(*_args, **kwargs):
            captured["min_similarity"] = kwargs.get("min_similarity")
            return [(0.95, "tag", "tag-hit")]

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, _hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.50,
                return_hits=False,
                knowledge_owner_id=None,
                strict_autoreply_gate=True,
            )

        self.assertTrue(ok)
        self.assertAlmostEqual(captured["min_similarity"], 0.50)

    async def test_is_relevant_uses_relevance_threshold_for_direct(self) -> None:
        captured = {}

        async def _fake_find_tag_hits(*_args, **kwargs):
            captured["min_similarity"] = kwargs.get("min_similarity")
            return [(0.35, "tag", "tag-hit")]

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, _hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.50,
                return_hits=False,
                knowledge_owner_id=None,
                strict_autoreply_gate=False,
            )

        self.assertTrue(ok)
        self.assertAlmostEqual(captured["min_similarity"], 0.28)

    async def test_is_relevant_returns_false_without_tag_hits(self) -> None:
        async def _fake_find_tag_hits(*_args, **_kwargs):
            return []

        with unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.1,
                return_hits=True,
                knowledge_owner_id=202,
            )

        self.assertFalse(ok)
        self.assertIsNone(hits)


if __name__ == "__main__":
    unittest.main()
