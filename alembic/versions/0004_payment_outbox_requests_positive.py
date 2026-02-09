#alembic/versions/0004_payment_outbox_requests_positive.py
from alembic import op
import sqlalchemy as sa

revision = "0004_payment_outbox_requests_positive"
down_revision = "0003_request_reservations"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT 1 FROM payment_outbox WHERE requests_amount <= 0 LIMIT 1")
    ).first()
    if existing:
        raise RuntimeError(
            "payment_outbox.requests_amount содержит значения <= 0; "
            "очистите данные перед миграцией."
        )

    op.drop_constraint("ck_payment_outbox_requests_nonneg", "payment_outbox", type_="check")
    op.create_check_constraint(
        "ck_payment_outbox_requests_positive",
        "payment_outbox",
        "requests_amount > 0",
    )


def downgrade():
    op.drop_constraint("ck_payment_outbox_requests_positive", "payment_outbox", type_="check")
    op.create_check_constraint(
        "ck_payment_outbox_requests_nonneg",
        "payment_outbox",
        "requests_amount >= 0",
    )
