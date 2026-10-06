"""A2A TaskStore (FR-A2A-SRV-06) — SQLite·PostgreSQL 공용.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("context_id", sa.String(64), nullable=False),
        sa.Column("skill", sa.String(128), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("status_message", sa.JSON(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_a2a_tasks_tenant_context", "a2a_tasks", ["tenant", "context_id"])


def downgrade() -> None:
    op.drop_index("ix_a2a_tasks_tenant_context", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
