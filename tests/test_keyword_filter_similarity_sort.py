import unittest

from app.services.responder.rag.keyword_filter import _sort_ids_by_similarity


class KeywordFilterSimilaritySortTests(unittest.TestCase):
    def test_sort_returns_all_ids_by_similarity_without_hard_cap(self):
        scores_by_id = {
            "x": 0.10,
            "y": 0.90,
            "z": 0.50,
            "w": 0.70,
        }

        ordered = _sort_ids_by_similarity(scores_by_id)

        self.assertEqual(ordered, ["y", "w", "z", "x"])
        self.assertEqual(set(ordered), set(scores_by_id.keys()))


if __name__ == "__main__":
    unittest.main()
