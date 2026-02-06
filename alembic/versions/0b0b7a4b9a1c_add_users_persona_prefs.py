#alembic/versions/0b0b7a4b9a1c_add_users_persona_prefs.py
from alembic import op

revision = "0b0b7a4b9a1c"
down_revision = "4b9a1c0b0b7a"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS persona_prefs JSONB
        DEFAULT '{}'::jsonb
    """)
    op.execute("""
        UPDATE users
        SET persona_prefs = '{}'::jsonb
        WHERE persona_prefs IS NULL
    """)
    op.execute("""
        ALTER TABLE users
        ALTER COLUMN persona_prefs SET NOT NULL
    """)
    op.execute("""
        ALTER TABLE users
        ALTER COLUMN persona_prefs DROP DEFAULT
    """)

def downgrade():
    op.execute("""
        ALTER TABLE users
        ALTER COLUMN persona_prefs DROP NOT NULL
    """)