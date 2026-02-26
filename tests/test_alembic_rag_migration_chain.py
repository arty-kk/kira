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
    def test_rag_revision_chain_down_revision_continuous_expected(self):
        migrations = _load_all_migrations()
        self.assertIsNone(migrations["0001_initial_schema"]["down_revision"])
        self.assertEqual(migrations["0002_fix_rag_hnsw_index"]["down_revision"], "0001_initial_schema")
        self.assertEqual(migrations["0003_rag_unique_tag_per_item"]["down_revision"], "0002_fix_rag_hnsw_index")
        self.assertEqual(migrations["0004_fix_rag_unique_indexes"]["down_revision"], "0003_rag_unique_tag_per_item")

    def test_head_migration_path_targets_hnsw_index_expected(self):
        migrations = _load_all_migrations()

        down_revisions = {
            down
            for data in migrations.values()
            for down in ([data["down_revision"]] if isinstance(data["down_revision"], str) else (data["down_revision"] or []))
            if down is not None
        }
        heads = [revision for revision in migrations if revision not in down_revisions]
        self.assertEqual(len(heads), 1)

        chain = []
        current = heads[0]
        while current is not None:
            chain.append(current)
            current = migrations[current]["down_revision"]
        chain.reverse()

        upgrade_sources = "\n".join(
            _upgrade_block(migrations[revision]["path"].read_text()) for revision in chain
        )

        self.assertIn("ix_rag_tag_vectors_embedding_cosine_hnsw", upgrade_sources)
        self.assertIn("DROP INDEX IF EXISTS ix_rag_tag_vectors_embedding_cosine_ann", upgrade_sources)

        create_ops = re.findall(
            r"CREATE INDEX IF NOT EXISTS (ix_rag_tag_vectors_embedding_cosine_(?:ann|hnsw))",
            upgrade_sources,
        )
        self.assertGreaterEqual(len(create_ops), 1)
        self.assertEqual(create_ops[-1], "ix_rag_tag_vectors_embedding_cosine_hnsw")


if __name__ == "__main__":
    unittest.main()
