"""MCP-REG 사설 레지스트리 (FR-MCP-REG-01·03·05·06).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("mcp_servers",
                    sa.Column("name", sa.String(200), primary_key=True), sa.Column("version", sa.String(64), primary_key=True),
                    sa.Column("server_json", sa.JSON(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("trust", sa.String(16), nullable=False), sa.Column("scan", sa.JSON(), nullable=True),
                    sa.Column("pins", sa.JSON(), nullable=True), sa.Column("protocol", sa.String(32), nullable=True),
                    sa.Column("created_at", ts, nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_table("mcp_allowlists",
                    sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("policy", sa.JSON(), nullable=False),
                    sa.Column("updated_at", ts, nullable=False))


def downgrade() -> None:
    op.drop_table("mcp_allowlists")
    op.drop_table("mcp_servers")
