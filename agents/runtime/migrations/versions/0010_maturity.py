"""MAT 성숙도 스냅샷.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("mat_snapshots", sa.Column("id", sa.String(36), primary_key=True), sa.Column("org", sa.String(128), nullable=False),
                    sa.Column("project", sa.String(128), nullable=False), sa.Column("level", sa.Integer(), nullable=False),
                    sa.Column("scores", sa.JSON(), nullable=False), sa.Column("facts", sa.JSON(), nullable=False),
                    sa.Column("kind", sa.String(16), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_mat_snap", "mat_snapshots", ["org", "project", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_mat_snap", "mat_snapshots")
    op.drop_table("mat_snapshots")
