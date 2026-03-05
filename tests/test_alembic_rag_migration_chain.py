import importlib.util
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS_DIR = ROOT / "alembic" / "versions"


def _load_migration_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"mig_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_all_migrations():
    migrations = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        module = _load_migration_module(path)
        migrations[module.revision] = {
            "path": path,
            "down_revision": module.down_revision,
        }
    return migrations


def _upgrade_block(source: str) -> str:
    match = re.search(r"def upgrade\(\):(?P<body>[\s\S]*?)\ndef downgrade\(\):", source)
    return match.group("body") if match else ""


class RagMigrationChainTests(unittest.TestCase):
    def test_single_root_migration_expected(self):
        migrations = _load_all_migrations()
        self.assertEqual(set(migrations.keys()), {"0001_initial_schema"})
        self.assertIsNone(migrations["0001_initial_schema"]["down_revision"])

    def test_initial_migration_contains_final_rag_indexes_expected(self):
        migrations = _load_all_migrations()
        upgrade_source = _upgrade_block(migrations["0001_initial_schema"]["path"].read_text())

        self.assertIn("ix_rag_tag_vectors_embedding_cosine_hnsw_large", upgrade_source)
        self.assertIn("embedding halfvec_cosine_ops", upgrade_source)
        self.assertIn("uq_rag_tag_vectors_global_item_tag", upgrade_source)
        self.assertIn("uq_rag_tag_vectors_auto_reply_item_tag", upgrade_source)
        self.assertIn("uq_rag_tag_vectors_owner_item_tag", upgrade_source)
        self.assertIn("ck_rag_tag_vectors_scope", upgrade_source)
        self.assertIn("scope IN ('global','auto_reply','owner')", upgrade_source)
        self.assertIn("ck_rag_tag_vectors_scope_owner_kb_consistency", upgrade_source)
        self.assertIn("scope IN ('global','auto_reply') AND owner_id IS NULL AND kb_id IS NULL", upgrade_source)
        self.assertIn("embedding_dim", upgrade_source)
        self.assertIn("ck_rag_tag_vectors_embedding_dim_fixed_3072", upgrade_source)


if __name__ == "__main__":
    unittest.main()
