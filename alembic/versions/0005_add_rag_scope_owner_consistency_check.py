from alembic import op
import sqlalchemy as sa

revision = "0005_add_rag_scope_owner_consistency_check"
down_revision = "0004_fix_rag_unique_indexes"
branch_labels = None
depends_on = None

_CONSISTENCY_RULE = (
    "(scope = 'global' AND owner_id IS NULL AND kb_id IS NULL) OR "
    "(scope = 'owner' AND owner_id IS NOT NULL AND kb_id IS NOT NULL)"
)
_CONSTRAINT_NAME = "ck_rag_tag_vectors_scope_owner_kb_consistency"


def upgrade():
    ctx = op.get_context()
    if not getattr(ctx, "as_sql", False):
        bind = op.get_bind()
        invalid_count = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM rag_tag_vectors WHERE NOT (" + _CONSISTENCY_RULE + ")"
            )
        ).scalar_one()
        ctx.config.print_stdout(
            "rag_tag_vectors invalid scope/owner/kb rows before cleanup: %s",
            invalid_count,
        )

    op.execute(
        "DELETE FROM rag_tag_vectors WHERE NOT (" + _CONSISTENCY_RULE + ");"
    )

    op.execute(
        "ALTER TABLE rag_tag_vectors "
        f"ADD CONSTRAINT {_CONSTRAINT_NAME} "
        "CHECK (" + _CONSISTENCY_RULE + ");"
    )


def downgrade():
    op.execute(
        "ALTER TABLE rag_tag_vectors "
        f"DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME};"
    )
