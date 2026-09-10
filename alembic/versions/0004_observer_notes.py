"""Attach bounded observer guidance to its existing shared journal entry."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable, no backfill/table rewrite. Existing table RLS and grants apply.
    op.add_column("memory_entries", sa.Column("observer_notes", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("memory_entries", "observer_notes")
