"""Shared memory notebooks and a searchable raw conversation journal."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_spaces",
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("space", sa.Text(), nullable=False),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "call_id", name="uq_memory_tenant_call"),
    )
    op.create_index("ix_memory_space_id", "memory_entries", ["tenant_id", "space", "id"])
    op.execute("CREATE INDEX ix_memory_search ON memory_entries USING gin (to_tsvector('simple', search_text))")
    for table in ("memory_spaces", "memory_entries"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_tenant ON {table} FOR ALL "
                   "USING (tenant_id = current_setting('app.tenant_id', true)::bigint) "
                   "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint)")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO dual_lobe_rls")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE memory_entries_id_seq TO dual_lobe_rls")


def downgrade():
    op.drop_table("memory_entries")
    op.drop_table("memory_spaces")
