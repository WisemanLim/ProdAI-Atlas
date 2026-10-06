"""X-SBX 실행 기록 · MCP 서버 헬스/SLA (MCP-SYS-06).

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("sbx_runs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("lang", sa.String(16), nullable=False), sa.Column("code_sha256", sa.String(64), nullable=False), sa.Column("code", sa.Text(), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False), sa.Column("exit_code", sa.Integer(), nullable=True), sa.Column("isolation", sa.String(16), nullable=False),
                    sa.Column("limits", sa.JSON(), nullable=False), sa.Column("egress", sa.JSON(), nullable=False), sa.Column("findings", sa.JSON(), nullable=False),
                    sa.Column("duration_ms", sa.Integer(), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_index("ix_sbx_runs_tenant", "sbx_runs", ["tenant", "created_at"])
    op.create_table("mcp_health", sa.Column("name", sa.String(200), primary_key=True), sa.Column("version", sa.String(64), primary_key=True),
                    sa.Column("state", sa.String(16), nullable=False), sa.Column("failures", sa.Integer(), nullable=False),
                    sa.Column("checks", sa.Integer(), nullable=False), sa.Column("errors", sa.Integer(), nullable=False),
                    sa.Column("latency_ms", sa.Integer(), nullable=True), sa.Column("last_error", sa.Text(), nullable=True), sa.Column("checked_at", ts, nullable=False))


def downgrade() -> None:
    op.drop_table("mcp_health")
    op.drop_index("ix_sbx_runs_tenant", "sbx_runs")
    op.drop_table("sbx_runs")
