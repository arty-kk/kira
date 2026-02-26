import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0005_add_rag_scope_owner_consistency_check.py"
CONSTRAINT_NAME = "ck_rag_tag_vectors_scope_owner_kb_consistency"
CONSISTENCY_RULE_SQL = (
    "(scope = 'global' AND owner_id IS NULL AND kb_id IS NULL) OR "
    "(scope = 'owner' AND owner_id IS NOT NULL AND kb_id IS NOT NULL)"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("mig_0005_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_has_expected_revision_and_constraint_sql():
    migration = _load_migration_module()
    source = MIGRATION_PATH.read_text()

    assert migration.revision == "0005_add_rag_scope_owner_consistency_check"
    assert migration.down_revision == "0004_fix_rag_unique_indexes"
    assert CONSTRAINT_NAME in source
    assert "SELECT COUNT(*) FROM rag_tag_vectors WHERE NOT" in source
    assert "DELETE FROM rag_tag_vectors WHERE NOT" in source
    assert "ADD CONSTRAINT" in source


@pytest.fixture()
def sqlite_conn():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE rag_tag_vectors ("
                "id INTEGER PRIMARY KEY, "
                "scope TEXT NOT NULL, "
                "owner_id BIGINT NULL, "
                "kb_id BIGINT NULL, "
                f"CONSTRAINT {CONSTRAINT_NAME} CHECK ({CONSISTENCY_RULE_SQL})"
                ");"
            )
        )
        yield conn


def test_constraint_allows_valid_scope_owner_kb_combinations(sqlite_conn):
    sqlite_conn.execute(
        sa.text("INSERT INTO rag_tag_vectors (scope, owner_id, kb_id) VALUES ('global', NULL, NULL)")
    )
    sqlite_conn.execute(
        sa.text("INSERT INTO rag_tag_vectors (scope, owner_id, kb_id) VALUES ('owner', 101, 202)")
    )


@pytest.mark.parametrize(
    "scope, owner_id, kb_id",
    [
        ("owner", None, 202),
        ("owner", 101, None),
        ("global", 101, None),
        ("global", None, 202),
        ("global", 101, 202),
    ],
)
def test_constraint_rejects_invalid_scope_owner_kb_combinations(sqlite_conn, scope, owner_id, kb_id):
    with pytest.raises(IntegrityError) as exc:
        sqlite_conn.execute(
            sa.text(
                "INSERT INTO rag_tag_vectors (scope, owner_id, kb_id) "
                "VALUES (:scope, :owner_id, :kb_id)"
            ),
            {"scope": scope, "owner_id": owner_id, "kb_id": kb_id},
        )

    assert "CHECK constraint failed" in str(exc.value)
    assert CONSTRAINT_NAME in str(exc.value)


def test_model_declares_same_named_check_constraint():
    source = (ROOT / "app" / "core" / "models.py").read_text()
    assert CONSTRAINT_NAME in source
    assert "scope = 'owner' AND owner_id IS NOT NULL AND kb_id IS NOT NULL" in source
