from alembic import op

revision = "0007_refund_outbox_billing_tier_check"
down_revision = "0006_refund_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE refund_outbox
        SET
            status = 'failed',
            last_error = 'invalid_billing_tier',
            processed_at = NULL,
            billing_tier = NULL,
            updated_at = now()
        WHERE billing_tier IS NOT NULL
          AND billing_tier NOT IN ('free', 'paid')
        """
    )
    op.create_check_constraint(
        "ck_refund_outbox_billing_tier",
        "refund_outbox",
        "billing_tier IN ('free','paid')",
    )


def downgrade():
    op.drop_constraint("ck_refund_outbox_billing_tier", "refund_outbox", type_="check")
