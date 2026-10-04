"""Subconscious baseline: MemorySpace.subconscious holds the space's running
"ordinary interaction" vector that ND-SMC variance is measured against.

Not memory content: no prompt ever reads this column, and it is not
reconstructable into anything a user said.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("memory_spaces", sa.Column(
        "subconscious", sa.Text(), nullable=False, server_default=""))


def downgrade():
    op.drop_column("memory_spaces", "subconscious")