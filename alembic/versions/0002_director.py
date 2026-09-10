"""Durable, tenant-isolated director sessions and short transaction leases."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "director_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("lease", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_director_tenant_run"),
    )
    op.execute("ALTER TABLE director_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY director_sessions_tenant ON director_sessions FOR ALL "
               "USING (tenant_id = current_setting('app.tenant_id', true)::bigint) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON director_sessions TO dual_lobe_rls")


def downgrade():
    op.drop_table("director_sessions")
