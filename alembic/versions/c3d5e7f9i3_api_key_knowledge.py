#alembic/versions/c3d5e7f9i3_api_key_knowledge.py
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c3d5e7f9i3"
down_revision = "b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_key_knowledge",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "api_key_id",
            sa.BigInteger(),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "embedding_model",
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("'text-embedding-3-large'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "chunks_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "api_key_id",
            "version",
            name="uq_api_key_knowledge_version",
        ),
    )

    op.create_index(
        "ix_api_key_knowledge_api_key_id",
        "api_key_knowledge",
        ["api_key_id"],
    )
    op.create_index(
        "ix_api_key_knowledge_status",
        "api_key_knowledge",
        ["status"],
    )


def downgrade():
    op.drop_index("ix_api_key_knowledge_status", table_name="api_key_knowledge")
    op.drop_index("ix_api_key_knowledge_api_key_id", table_name="api_key_knowledge")
    op.drop_table("api_key_knowledge")