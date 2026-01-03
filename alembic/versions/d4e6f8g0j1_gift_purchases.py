#alembic/versions/d4e6f8g0j1_gift_purchases.py
from alembic import op
import sqlalchemy as sa

revision = "d4e6f8g0j1"
down_revision = "c3d5e7f9i3"
branch_labels = None
depends_on = None


def upgrade():
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
        sa.Column("telegram_payment_charge_id", sa.String(length=128), nullable=False, unique=True),
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
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),  # buy|gift
        sa.Column("requests_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stars_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("invoice_payload", sa.String(length=128), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.String(length=128), nullable=False, unique=True),
        sa.Column("provider_payment_charge_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
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
    op.drop_index("ix_gift_purchases_created_at", table_name="gift_purchases")
    op.drop_index("ix_gift_purchases_user_id", table_name="gift_purchases")
    op.drop_table("gift_purchases")
    op.drop_index("ix_payment_receipts_provider_payment_charge_id", table_name="payment_receipts")
    op.drop_index("ix_payment_receipts_created_at", table_name="payment_receipts")
    op.drop_index("ix_payment_receipts_user_id", table_name="payment_receipts")
    op.drop_table("payment_receipts")