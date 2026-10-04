"""Note embeddings: an in-perimeter candidate lane for episodic recall.

memory_notes gains an embedding column (JSONB float vector from the
active in-perimeter embedder, [] when unembedded). The chord remains the
decider; this only widens candidate generation beyond the boolean
word-overlap query. Rows with a length-mismatched vector (embedder
swap) are treated as unembedded.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("memory_notes", sa.Column(
        "embedding", sa.dialects.postgresql.JSONB(), nullable=False,
        server_default=sa.text("'[]'::jsonb")))


def downgrade():
    op.drop_column("memory_notes", "embedding")