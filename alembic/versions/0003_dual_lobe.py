"""Dual-lobe mode ring-buffer store (isolated from memory_entries)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dual_lobe_entries",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_dual_lobe_tenant_seq", "dual_lobe_entries", ["tenant_id", "seq"])
    op.execute("ALTER TABLE dual_lobe_entries ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY dual_lobe_entries_tenant ON dual_lobe_entries FOR ALL "
               "USING (tenant_id = current_setting('app.tenant_id', true)::bigint) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON dual_lobe_entries TO dual_lobe_rls")


def downgrade():
    op.drop_index("ix_dual_lobe_tenant_seq", table_name="dual_lobe_entries")
    op.drop_table("dual_lobe_entries")
