"""add unique refund key for refund_outbox

Revision ID: 0002_refund_outbox_refund_key_unique
Revises: 0001_initial_schema
Create Date: 2026-02-12 00:00:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0002_refund_outbox_refund_key_unique"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY request_id, reason
                    ORDER BY
                        CASE status
                            WHEN 'applied' THEN 0
                            WHEN 'pending' THEN 1
                            ELSE 2
                        END,
                        id
                ) AS rn
            FROM refund_outbox
        )
        DELETE FROM refund_outbox ro
        USING ranked r
        WHERE ro.id = r.id
          AND r.rn > 1
        """
    )

    op.create_unique_constraint(
        "uq_refund_outbox_request_reason",
        "refund_outbox",
        ["request_id", "reason"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_refund_outbox_request_reason", "refund_outbox", type_="unique")
