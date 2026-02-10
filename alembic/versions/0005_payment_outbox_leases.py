from alembic import op
import sqlalchemy as sa

revision = "0005_payment_outbox_leases"
down_revision = "0004_payment_outbox_requests_positive"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment_outbox", sa.Column("lease_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("payment_outbox", sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_outbox", sa.Column("lease_token", sa.String(length=64), nullable=True))

    op.create_index("ix_payment_outbox_leased_at", "payment_outbox", ["leased_at"], unique=False)
    op.create_check_constraint(
        "ck_payment_outbox_lease_attempts_nonneg",
        "payment_outbox",
        "lease_attempts >= 0",
    )


def downgrade():
    op.drop_constraint("ck_payment_outbox_lease_attempts_nonneg", "payment_outbox", type_="check")
    op.drop_index("ix_payment_outbox_leased_at", table_name="payment_outbox")

    op.drop_column("payment_outbox", "lease_token")
    op.drop_column("payment_outbox", "leased_at")
    op.drop_column("payment_outbox", "lease_attempts")
