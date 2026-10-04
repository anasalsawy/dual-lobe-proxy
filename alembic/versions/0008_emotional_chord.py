"""Emotional chord: concurrent affect as a vector on each memory note.

memory_notes gains a chord column (JSONB dict over a fixed axis
vocabulary). The feeling tag stays as the human-readable rendering; the
chord is what associative recall compares. Old rows keep {} and fall
back to tag-based recall until C re-observes them.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("memory_notes", sa.Column(
        "chord", sa.dialects.postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("memory_notes", "chord")