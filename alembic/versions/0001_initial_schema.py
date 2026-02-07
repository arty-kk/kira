#alembic/versions/0001_initial_schema.py
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "free_requests",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
        sa.Column(
            "paid_requests",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "used_requests",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("gender", sa.String(length=6), nullable=True),
        sa.Column(
            "total_paid_cents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("pm_welcome_sent", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "persona_prefs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column(
            "persona_prefs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"])

    op.create_table(
        "api_key_stats",
        sa.Column(
            "api_key_id",
            sa.BigInteger(),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "messages_in",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "messages_out",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_latency_ms",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.create_table(
        "api_key_knowledge",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
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
            server_default=sa.text("1"),
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
        "ix_api_key_knowledge_created_at",
        "api_key_knowledge",
        ["created_at"],
    )

    op.create_table(
        "gift_purchases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gift_code", sa.String(length=64), nullable=False),
        sa.Column("gift_title", sa.String(length=128), nullable=True),
        sa.Column("gift_emoji", sa.String(length=32), nullable=True),
        sa.Column("stars_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("requests_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("invoice_payload", sa.String(length=128), nullable=True),
        sa.Column(
            "telegram_payment_charge_id",
            sa.String(length=128),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("gift_code <> ''", name="ck_gift_purchases_gift_code_nonempty"),
        sa.CheckConstraint("stars_amount >= 0", name="ck_gift_purchases_stars_nonneg"),
        sa.CheckConstraint("requests_amount >= 0", name="ck_gift_purchases_requests_nonneg"),
        sa.CheckConstraint("telegram_payment_charge_id <> ''", name="ck_gift_purchases_charge_id_nonempty"),
    )
    op.create_index("ix_gift_purchases_user_id", "gift_purchases", ["user_id"])
    op.create_index("ix_gift_purchases_created_at", "gift_purchases", ["created_at"])

    op.create_table(
        "payment_receipts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("requests_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stars_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("invoice_payload", sa.String(length=128), nullable=True),
        sa.Column(
            "telegram_payment_charge_id",
            sa.String(length=128),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider_payment_charge_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('buy','gift')", name="ck_payment_receipts_kind"),
        sa.CheckConstraint("stars_amount >= 0", name="ck_payment_receipts_stars_nonneg"),
        sa.CheckConstraint("requests_amount >= 0", name="ck_payment_receipts_requests_nonneg"),
        sa.CheckConstraint("telegram_payment_charge_id <> ''", name="ck_payment_receipts_charge_id_nonempty"),
        sa.CheckConstraint(
            "(provider_payment_charge_id IS NULL) OR (provider_payment_charge_id <> '')",
            name="ck_payment_receipts_provider_charge_id_nonempty",
        ),
    )
    op.create_index("ix_payment_receipts_user_id", "payment_receipts", ["user_id"])
    op.create_index("ix_payment_receipts_created_at", "payment_receipts", ["created_at"])
    op.create_index(
        "ix_payment_receipts_provider_payment_charge_id",
        "payment_receipts",
        ["provider_payment_charge_id"],
    )


def downgrade():
    op.drop_table("payment_receipts")
    op.drop_table("gift_purchases")
    op.drop_table("api_key_knowledge")
    op.drop_table("api_key_stats")
    op.drop_index("ix_api_keys_created_at", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("users")
