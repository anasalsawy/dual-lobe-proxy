"""Companion interactions: surface provenance, unaddressed queue, verdict memory.

memory_notes gains surface provenance (where the user said it - never memory
content) and resurfaced_at (when a note last reached A), so never-resurfaced
asides can be found again. verdict_snapshots stores the data line's own
conclusion per turn so a later turn can notice the system changed its mind.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("memory_notes", sa.Column("surface_device", sa.Text(), nullable=False, server_default=""))
    op.add_column("memory_notes", sa.Column("surface_app", sa.Text(), nullable=False, server_default=""))
    op.add_column("memory_notes", sa.Column("resurfaced_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "verdict_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("space", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("topic_terms", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_verdict_space_id", "verdict_snapshots", ["tenant_id", "space", "id"])
    op.execute("CREATE INDEX ix_verdict_terms ON verdict_snapshots USING gin (to_tsvector('simple', topic_terms))")
    op.execute("ALTER TABLE verdict_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY verdict_snapshots_tenant ON verdict_snapshots FOR ALL "
               "USING (tenant_id = current_setting('app.tenant_id', true)::bigint) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON verdict_snapshots TO dual_lobe_rls")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE verdict_snapshots_id_seq TO dual_lobe_rls")


def downgrade():
    op.drop_table("verdict_snapshots")
    op.drop_column("memory_notes", "resurfaced_at")
    op.drop_column("memory_notes", "surface_app")
    op.drop_column("memory_notes", "surface_device")