#alembic/versions/0004_fix_rag_unique_indexes.py
from alembic import op

revision = "0004_fix_rag_unique_indexes"
down_revision = "0003_rag_unique_tag_per_item"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE rag_tag_vectors "
        "DROP CONSTRAINT IF EXISTS uq_rag_tag_vectors_item_tag;"
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_tag_vectors_global_item_tag "
        "ON rag_tag_vectors (embedding_model, external_id, tag) "
        "WHERE scope = 'global';"
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_tag_vectors_owner_item_tag "
        "ON rag_tag_vectors (embedding_model, owner_id, kb_id, external_id, tag) "
        "WHERE scope = 'owner';"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_rag_tag_vectors_owner_item_tag;")
    op.execute("DROP INDEX IF EXISTS uq_rag_tag_vectors_global_item_tag;")

    op.execute(
        "ALTER TABLE rag_tag_vectors "
        "ADD CONSTRAINT uq_rag_tag_vectors_item_tag "
        "UNIQUE (embedding_model, scope, owner_id, kb_id, external_id, tag);"
    )
