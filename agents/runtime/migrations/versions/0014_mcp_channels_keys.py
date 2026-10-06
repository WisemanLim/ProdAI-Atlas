"""MCP-REG 버전 채널(REG-07) · 게시자 서명 키(REG-04) · MCP 게이트웨이 가상 서버(HOST-04).

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("mcp_channels", sa.Column("name", sa.String(200), primary_key=True), sa.Column("channel", sa.String(16), primary_key=True),
                    sa.Column("version", sa.String(64), nullable=False), sa.Column("history", sa.JSON(), nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_table("mcp_keys", sa.Column("kid", sa.String(64), primary_key=True), sa.Column("public_key", sa.Text(), nullable=False),
                    sa.Column("owner", sa.String(128), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("mcp_virtual_servers", sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("name", sa.String(64), primary_key=True),
                    sa.Column("include", sa.JSON(), nullable=False), sa.Column("exclude", sa.JSON(), nullable=False), sa.Column("profile", sa.String(64)),
                    sa.Column("updated_at", ts, nullable=False))


def downgrade() -> None:
    op.drop_table("mcp_virtual_servers")
    op.drop_table("mcp_keys")
    op.drop_table("mcp_channels")
