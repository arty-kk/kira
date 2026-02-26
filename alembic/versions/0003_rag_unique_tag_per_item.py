#alembic/versions/0003_rag_unique_tag_per_item.py
from alembic import op

revision = "0003_rag_unique_tag_per_item"
down_revision = "0002_fix_rag_hnsw_index"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE rag_tag_vectors "
        "ADD CONSTRAINT uq_rag_tag_vectors_item_tag "
        "UNIQUE (embedding_model, scope, owner_id, kb_id, external_id, tag);"
    )


def downgrade():
    op.execute(
        "ALTER TABLE rag_tag_vectors "
        "DROP CONSTRAINT IF EXISTS uq_rag_tag_vectors_item_tag;"
    )
