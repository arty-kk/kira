import unittest

from app.services.responder.rag import knowledge_proc


class KnowledgeProcHelpersTests(unittest.TestCase):
    def test_normalize_embedding_1d_rejects_empty_and_non_finite(self):
        self.assertIsNone(knowledge_proc._normalize_embedding_1d([]))
        self.assertIsNone(knowledge_proc._normalize_embedding_1d([1.0, float("nan")]))

    def test_normalize_embedding_1d_flattens_single_row_matrix(self):
        out = knowledge_proc._normalize_embedding_1d([[1.0, 2.0, 3.0]])
        self.assertIsNotNone(out)
        self.assertEqual(out.tolist(), [1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
