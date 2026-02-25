import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_init_migration_module():
    path = ROOT / "alembic" / "versions" / "0001_initial_schema.py"
    spec = importlib.util.spec_from_file_location("mig_init_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_init_migration_module()


class InitialMigrationTests(unittest.TestCase):
    def test_only_single_init_migration_exists(self):
        versions = sorted((ROOT / "alembic" / "versions").glob("*.py"))
        self.assertEqual([p.name for p in versions], ["0001_initial_schema.py"])

    def test_init_migration_is_root_revision(self):
        self.assertEqual(migration.revision, "0001_initial_schema")
        self.assertIsNone(migration.down_revision)

    def test_init_migration_contains_rag_hnsw_index(self):
        source = (ROOT / "alembic" / "versions" / "0001_initial_schema.py").read_text()
        self.assertIn("ix_rag_tag_vectors_embedding_cosine_ann", source)
        self.assertIn("USING hnsw", source)


if __name__ == "__main__":
    unittest.main()
