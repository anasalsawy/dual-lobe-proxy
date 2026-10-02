"""Companion identity: the API key carries a persona.

Any surface presenting a key resolves to the same persona and therefore shares
one memory. Empty string means the key has no persona scope and behaves as
before (memory falls back to the caller-chosen space).
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "api_keys",
        sa.Column("persona", sa.Text(), nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("api_keys", "persona")
