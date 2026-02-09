#alembic/versions/0003_request_reservations.py
from alembic import op
import sqlalchemy as sa

revision = "0003_request_reservations"
down_revision = "0002_payment_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "request_reservations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column("used_paid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('reserved','consumed','refunded')",
            name="ck_request_reservations_status",
        ),
    )
    op.create_index("ix_request_reservations_user_id", "request_reservations", ["user_id"])
    op.create_index("ix_request_reservations_created_at", "request_reservations", ["created_at"])


def downgrade():
    op.drop_index("ix_request_reservations_created_at", table_name="request_reservations")
    op.drop_index("ix_request_reservations_user_id", table_name="request_reservations")
    op.drop_table("request_reservations")
