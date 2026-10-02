"""Episodic memory: personal/emotional notes B attaches to events.

A small, verbatim-quoted, salience-weighted note the user made, anchored to the
topic or event in progress, so it can be recalled when that topic returns.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_notes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("space", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="detail"),
        sa.Column("anchor", sa.Text(), nullable=False, server_default=""),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("feeling", sa.Text(), nullable=False, server_default="neutral"),
        sa.Column("salience", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('affect','preference','callback','plan','detail')", name="ck_note_kind"),
    )
    op.create_index("ix_note_space_id", "memory_notes", ["tenant_id", "space", "id"])
    op.execute("CREATE INDEX ix_note_search ON memory_notes USING gin (to_tsvector('simple', search_text))")
    op.execute("ALTER TABLE memory_notes ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY memory_notes_tenant ON memory_notes FOR ALL "
               "USING (tenant_id = current_setting('app.tenant_id', true)::bigint) "
               "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::bigint)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memory_notes TO dual_lobe_rls")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE memory_notes_id_seq TO dual_lobe_rls")


def downgrade():
    op.drop_table("memory_notes")
