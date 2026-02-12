import unittest
import unittest.mock

from app.services.responder.rag import relevance


class RelevanceKnowledgeOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_relevant_prefers_knowledge_owner_id_for_custom_kb(self) -> None:
        captured_owner_ids = []

        async def _fake_find_tag_hits(*_args, **kwargs):
            captured_owner_ids.append(kwargs.get("owner_id"))
            return []

        async def _fake_get_relevant(*_args, **_kwargs):
            return []

        async def _fake_get_relevant_for_owner(*_args, **kwargs):
            captured_owner_ids.append(kwargs.get("owner_id"))
            return [(0.99, "c", "custom")]

        with (
            unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits),
            unittest.mock.patch.object(relevance, "get_relevant", _fake_get_relevant),
            unittest.mock.patch.object(relevance, "get_relevant_for_owner", _fake_get_relevant_for_owner),
        ):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.1,
                return_hits=True,
                persona_owner_id=101,
                knowledge_owner_id=202,
            )

        self.assertTrue(ok)
        self.assertEqual(captured_owner_ids, [202, 202])
        self.assertEqual(hits, [(0.99, "c", "custom")])

    async def test_is_relevant_skips_owner_paths_when_knowledge_owner_missing(self) -> None:
        captured_owner_ids = []
        owner_calls = 0

        async def _fake_find_tag_hits(*_args, **kwargs):
            captured_owner_ids.append(kwargs.get("owner_id"))
            return []

        async def _fake_get_relevant(*_args, **_kwargs):
            return [(0.95, "s", "system")]

        async def _fake_get_relevant_for_owner(*_args, **_kwargs):
            nonlocal owner_calls
            owner_calls += 1
            return [(0.99, "c", "custom")]

        with (
            unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits),
            unittest.mock.patch.object(relevance, "get_relevant", _fake_get_relevant),
            unittest.mock.patch.object(relevance, "get_relevant_for_owner", _fake_get_relevant_for_owner),
        ):
            ok, hits = await relevance.is_relevant(
                "hello",
                model="test-model",
                threshold=0.1,
                return_hits=True,
                persona_owner_id=101,
                knowledge_owner_id=None,
            )

        self.assertTrue(ok)
        self.assertEqual(captured_owner_ids, [None])
        self.assertEqual(owner_calls, 0)
        self.assertEqual(hits, [(0.95, "s", "system")])

    async def test_is_relevant_skips_owner_paths_when_knowledge_owner_invalid(self) -> None:
        invalid_values = [0, -1, "abc"]

        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                captured_owner_ids = []
                owner_calls = 0

                async def _fake_find_tag_hits(*_args, **kwargs):
                    captured_owner_ids.append(kwargs.get("owner_id"))
                    return []

                async def _fake_get_relevant(*_args, **_kwargs):
                    return [(0.95, "s", "system")]

                async def _fake_get_relevant_for_owner(*_args, **_kwargs):
                    nonlocal owner_calls
                    owner_calls += 1
                    return [(0.99, "c", "custom")]

                with (
                    unittest.mock.patch.object(relevance, "find_tag_hits", _fake_find_tag_hits),
                    unittest.mock.patch.object(relevance, "get_relevant", _fake_get_relevant),
                    unittest.mock.patch.object(relevance, "get_relevant_for_owner", _fake_get_relevant_for_owner),
                ):
                    ok, hits = await relevance.is_relevant(
                        "hello",
                        model="test-model",
                        threshold=0.1,
                        return_hits=True,
                        persona_owner_id=101,
                        knowledge_owner_id=invalid_value,
                    )

                self.assertTrue(ok)
                self.assertEqual(captured_owner_ids, [None])
                self.assertEqual(owner_calls, 0)
                self.assertEqual(hits, [(0.95, "s", "system")])


if __name__ == "__main__":
    unittest.main()
