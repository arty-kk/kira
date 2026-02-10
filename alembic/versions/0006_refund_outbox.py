from alembic import op
import sqlalchemy as sa

revision = "0006_refund_outbox"
down_revision = "0005_payment_outbox_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "refund_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("billing_tier", sa.String(length=16), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refund_outbox_owner_id", "refund_outbox", ["owner_id"], unique=False)
    op.create_index("ix_refund_outbox_request_id", "refund_outbox", ["request_id"], unique=False)
    op.create_index("ix_refund_outbox_leased_at", "refund_outbox", ["leased_at"], unique=False)
    op.create_index("ix_refund_outbox_created_at", "refund_outbox", ["created_at"], unique=False)
    op.create_index("ix_refund_outbox_processed_at", "refund_outbox", ["processed_at"], unique=False)

    op.create_check_constraint("ck_refund_outbox_status", "refund_outbox", "status IN ('pending','applied','failed')")
    op.create_check_constraint("ck_refund_outbox_attempts_nonneg", "refund_outbox", "attempts >= 0")
    op.create_check_constraint("ck_refund_outbox_lease_attempts_nonneg", "refund_outbox", "lease_attempts >= 0")
    op.create_check_constraint("ck_refund_outbox_request_id_nonempty", "refund_outbox", "request_id <> ''")
    op.create_check_constraint("ck_refund_outbox_reason_nonempty", "refund_outbox", "reason <> ''")


def downgrade():
    op.drop_constraint("ck_refund_outbox_reason_nonempty", "refund_outbox", type_="check")
    op.drop_constraint("ck_refund_outbox_request_id_nonempty", "refund_outbox", type_="check")
    op.drop_constraint("ck_refund_outbox_lease_attempts_nonneg", "refund_outbox", type_="check")
    op.drop_constraint("ck_refund_outbox_attempts_nonneg", "refund_outbox", type_="check")
    op.drop_constraint("ck_refund_outbox_status", "refund_outbox", type_="check")

    op.drop_index("ix_refund_outbox_processed_at", table_name="refund_outbox")
    op.drop_index("ix_refund_outbox_created_at", table_name="refund_outbox")
    op.drop_index("ix_refund_outbox_leased_at", table_name="refund_outbox")
    op.drop_index("ix_refund_outbox_request_id", table_name="refund_outbox")
    op.drop_index("ix_refund_outbox_owner_id", table_name="refund_outbox")
    op.drop_table("refund_outbox")
