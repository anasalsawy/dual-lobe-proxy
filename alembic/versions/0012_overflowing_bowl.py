"""Overflowing bowl: tiers instead of purge.

raw_interactions gains the lossy tier (compressed_transcript/compressed_at)
the bake state (baked: training consumed the export), and the byte
accounting the rim trigger reads (raw_chars/compressed_chars).
Backfill sizes from live text so existing rows evict on real weight.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("raw_interactions", sa.Column(
        "compressed_transcript", sa.Text(), nullable=False, server_default=sa.text("''")))
    op.add_column("raw_interactions", sa.Column(
        "raw_chars", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("raw_interactions", sa.Column(
        "compressed_chars", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("raw_interactions", sa.Column(
        "compressed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("raw_interactions", sa.Column(
        "baked", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.execute(sa.text(
        "UPDATE raw_interactions SET raw_chars = octet_length(raw_transcript)"))


def downgrade():
    op.drop_column("raw_interactions", "baked")
    op.drop_column("raw_interactions", "compressed_at")
    op.drop_column("raw_interactions", "compressed_chars")
    op.drop_column("raw_interactions", "raw_chars")
    op.drop_column("raw_interactions", "compressed_transcript")