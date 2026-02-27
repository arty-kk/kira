import unittest

from app.services.responder.rag.keyword_filter import _mmr_select_ids


class KeywordFilterMmrTests(unittest.TestCase):
    def test_mmr_uses_negative_cosine_domain(self):
        cand_ids = ["a", "b"]
        # b is anti-correlated with a and should not be over-penalized as if sim=0.
        vecs_by_id = {
            "a": [1.0, 0.0],
            "b": [-1.0, 0.0],
        }
        scores_by_id = {"a": 0.9, "b": 0.8}

        picked = _mmr_select_ids(cand_ids, vecs_by_id, scores_by_id, top_k=2, lam=0.1)

        self.assertEqual(picked, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
