"""ND-SMC buffer: raw_interactions - high-variance turns, kept raw or not at all.

Phase 1/2 storage. raw_transcript is never summarized or normalized by any
code path; low-variance turns never reach this table. locked marks rows the
consolidation job has exported to a training file - the purge deletes only
locked rows.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "raw_interactions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("space", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("raw_transcript", sa.Text(), nullable=False),
        sa.Column("style_markers", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("embedding", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("variance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_raw_space_id", "raw_interactions", ["tenant_id", "space", "id"])
    op.execute("ALTER TABLE raw_interactions ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY raw_interactions_tenant ON raw_interactions FOR ALL "
               "USING (tenant_id = current_setting('app.tenant_id', true)::bigint) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON raw_interactions TO dual_lobe_rls")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE raw_interactions_id_seq TO dual_lobe_rls")


def downgrade():
    op.drop_table("raw_interactions")