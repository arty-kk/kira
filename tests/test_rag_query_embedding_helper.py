import unittest

from app.services.responder.rag.query_embedding import normalize_query_embedding


class QueryEmbeddingNormalizeTests(unittest.TestCase):
    def test_valid_list_passes(self):
        vec = [1.0, 2.0, 3.0]
        result = normalize_query_embedding(vec, expected_dim=3)
        self.assertEqual(result, vec)

    def test_singleton_2d_is_flattened(self):
        result = normalize_query_embedding([[1.0, 2.0, 3.0]], expected_dim=3)
        self.assertEqual(result, [1.0, 2.0, 3.0])

    def test_non_finite_rejected(self):
        self.assertIsNone(normalize_query_embedding([float("nan"), 1.0], expected_dim=2))
        self.assertIsNone(normalize_query_embedding([float("inf"), 1.0], expected_dim=2))

    def test_wrong_dim_rejected(self):
        self.assertIsNone(normalize_query_embedding([1.0, 2.0], expected_dim=3))

    def test_invalid_expected_dim_rejected_without_exception(self):
        self.assertIsNone(normalize_query_embedding([1.0, 2.0], expected_dim=None))


if __name__ == "__main__":
    unittest.main()
