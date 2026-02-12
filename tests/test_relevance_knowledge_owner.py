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


if __name__ == "__main__":
    unittest.main()
