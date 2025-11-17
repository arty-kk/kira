#alembic/versions/4b9a1c0b0b7a_add_pm_welcome_sent copy.py
from alembic import op
import sqlalchemy as sa

revision = "4b9a1c0b0b7a"
down_revision = "3a6f7b8c6a9e"
branch_labels = None
depends_on = None

def upgrade():
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pm_welcome_sent TIMESTAMPTZ NULL"
    )

def downgrade():
    pass