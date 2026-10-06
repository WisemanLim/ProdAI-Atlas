"""A2A Push 설정 (FR-A2A-SRV-03) · 원격 위임 Task 미러 (FR-A2A-CLI-04).

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("a2a_push_configs", sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("task_id", sa.String(64), primary_key=True),
                    sa.Column("id", sa.String(64), primary_key=True), sa.Column("url", sa.String(1024), nullable=False),
                    sa.Column("token", sa.String(256), nullable=True), sa.Column("created_at", ts, nullable=False))
    op.create_table("a2a_remote_tasks", sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("remote_task_id", sa.String(64), primary_key=True),
                    sa.Column("agent_id", sa.String(64), nullable=False), sa.Column("agent_name", sa.String(128), nullable=False),
                    sa.Column("parent", sa.String(128), nullable=True), sa.Column("skill", sa.String(128), nullable=True),
                    sa.Column("state", sa.String(40), nullable=False), sa.Column("detail", sa.JSON(), nullable=True),
                    sa.Column("created_at", ts, nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_index("ix_a2a_remote_parent", "a2a_remote_tasks", ["tenant", "parent"])


def downgrade() -> None:
    op.drop_index("ix_a2a_remote_parent", "a2a_remote_tasks")
    op.drop_table("a2a_remote_tasks")
    op.drop_table("a2a_push_configs")
