from alembic import op

revision = "0002_fix_rag_hnsw_index"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP INDEX IF EXISTS ix_rag_tag_vectors_embedding_cosine_ann;")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_tag_vectors_embedding_cosine_hnsw "
        "ON rag_tag_vectors USING hnsw (embedding vector_cosine_ops);"
    )
  
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_tag_vectors_filter "
        "ON rag_tag_vectors (embedding_model, scope, owner_id, kb_id);"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_rag_tag_vectors_filter;")
    op.execute("DROP INDEX IF EXISTS ix_rag_tag_vectors_embedding_cosine_hnsw;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rag_tag_vectors_embedding_cosine_ann "
        "ON rag_tag_vectors USING hnsw ((CAST(embedding AS halfvec(3072))) halfvec_cosine_ops);"
    )
