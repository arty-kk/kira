# alembic/versions/a1b2c3d4e5f6_keep_default_persona_prefs.py
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "0b0b7a4b9a1c"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("UPDATE users SET persona_prefs = '{}'::jsonb WHERE persona_prefs IS NULL;")
    op.execute("ALTER TABLE users ALTER COLUMN persona_prefs SET DEFAULT '{}'::jsonb;")
    op.execute("ALTER TABLE users ALTER COLUMN persona_prefs SET NOT NULL;")

def downgrade():
    op.execute("ALTER TABLE users ALTER COLUMN persona_prefs DROP DEFAULT;")