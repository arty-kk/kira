import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core import embedding_utils
from app.core.embedding_utils import resolve_embedding_dim


class ResolveEmbeddingDimTests(unittest.TestCase):
    def test_known_openai_models(self):
        self.assertEqual(resolve_embedding_dim("text-embedding-3-small", fallback_dim=3072), 1536)
        self.assertEqual(resolve_embedding_dim("text-embedding-3-large", fallback_dim=1536), 3072)

    def test_unknown_model_uses_fallback(self):
        self.assertEqual(resolve_embedding_dim("custom-model", fallback_dim=2048), 2048)


class GetRagEmbeddingModelTests(unittest.TestCase):
    def test_explicit_model_has_priority(self):
        with patch.object(embedding_utils, "settings", SimpleNamespace(EMBEDDING_MODEL="text-embedding-3-large")):
            self.assertEqual(embedding_utils.get_rag_embedding_model("text-embedding-3-small"), "text-embedding-3-small")

    def test_fallbacks_to_global_embedding_model(self):
        with patch.object(embedding_utils, "settings", SimpleNamespace(EMBEDDING_MODEL="text-embedding-3-small")):
            self.assertEqual(embedding_utils.get_rag_embedding_model(), "text-embedding-3-small")


if __name__ == "__main__":
    unittest.main()
