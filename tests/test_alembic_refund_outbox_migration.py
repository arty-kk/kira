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
    def test_initial_schema_migration_file_exists_expected(self):
        versions = sorted((ROOT / "alembic" / "versions").glob("*.py"))
        names = [p.name for p in versions]
        self.assertIn("0001_initial_schema.py", names)

    def test_initial_schema_migration_is_root_revision_expected(self):
        self.assertEqual(migration.revision, "0001_initial_schema")
        self.assertIsNone(migration.down_revision)

    def test_initial_schema_refund_outbox_schema_exists_expected(self):
        source = (ROOT / "alembic" / "versions" / "0001_initial_schema.py").read_text()
        self.assertIn("refund_outbox", source)
        self.assertIn("uq_refund_outbox_request_id", source)
        self.assertIn("ix_refund_outbox_request_id", source)


if __name__ == "__main__":
    unittest.main()
