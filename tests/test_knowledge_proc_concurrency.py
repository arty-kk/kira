import unittest

import numpy as np

from app.services.responder.rag import knowledge_proc


class KnowledgeProcHelpersTests(unittest.TestCase):
    def test_mmr_select_picks_unique_items(self):
        E = np.asarray([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32)
        scores = np.asarray([0.9, 0.8, 0.7], dtype=np.float32)
        picked = knowledge_proc._mmr_select(E, scores, top_k=2, lam=0.55)
        self.assertEqual(len(picked), 2)
        self.assertEqual(len(set(picked)), 2)


if __name__ == "__main__":
    unittest.main()
