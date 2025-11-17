#alembic/versions/3a6f7b8c6a9e_initial_users.py
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "3a6f7b8c6a9e"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("registered_at",sa.DateTime(timezone=True),server_default=sa.text("now()"),nullable=True,),
        sa.Column("free_requests", sa.Integer(), nullable=False),
        sa.Column("paid_requests", sa.Integer(), nullable=False),
        sa.Column("used_requests", sa.Integer(), nullable=False),
        sa.Column("total_paid_cents", sa.Integer(), nullable=False),
        sa.Column("pm_welcome_sent", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gender", sa.String(length=6), nullable=True),
        sa.Column("persona_prefs", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_index("ix_users_username", "users", ["username"], unique=False)

def downgrade():
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")