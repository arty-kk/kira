#alembic/versions/0002_payment_outbox.py
from alembic import op
import sqlalchemy as sa

revision = "0002_payment_outbox"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("requests_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stars_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("invoice_payload", sa.String(length=128), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.String(length=128), nullable=False),
        sa.Column("provider_payment_charge_id", sa.String(length=128), nullable=True),
        sa.Column("gift_code", sa.String(length=64), nullable=True),
        sa.Column("gift_title", sa.String(length=128), nullable=True),
        sa.Column("gift_emoji", sa.String(length=32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('buy','gift')", name="ck_payment_outbox_kind"),
        sa.CheckConstraint(
            "status IN ('pending','applied','failed')",
            name="ck_payment_outbox_status",
        ),
        sa.CheckConstraint("stars_amount >= 0", name="ck_payment_outbox_stars_nonneg"),
        sa.CheckConstraint("requests_amount >= 0", name="ck_payment_outbox_requests_nonneg"),
        sa.CheckConstraint(
            "telegram_payment_charge_id <> ''",
            name="ck_payment_outbox_charge_id_nonempty",
        ),
        sa.UniqueConstraint("telegram_payment_charge_id", name="uq_payment_outbox_charge_id"),
    )
    op.create_index("ix_payment_outbox_user_id", "payment_outbox", ["user_id"])
    op.create_index("ix_payment_outbox_created_at", "payment_outbox", ["created_at"])
    op.create_index("ix_payment_outbox_applied_at", "payment_outbox", ["applied_at"])
    op.create_index(
        "ix_payment_outbox_provider_payment_charge_id",
        "payment_outbox",
        ["provider_payment_charge_id"],
    )


def downgrade():
    op.drop_index("ix_payment_outbox_provider_payment_charge_id", table_name="payment_outbox")
    op.drop_index("ix_payment_outbox_applied_at", table_name="payment_outbox")
    op.drop_index("ix_payment_outbox_created_at", table_name="payment_outbox")
    op.drop_index("ix_payment_outbox_user_id", table_name="payment_outbox")
    op.drop_table("payment_outbox")
