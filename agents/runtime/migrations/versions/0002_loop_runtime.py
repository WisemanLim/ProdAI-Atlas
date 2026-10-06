"""AL-RUN 루프 런타임 — 런·체크포인트·이벤트·멱등·아티팩트 (FR-AL-RUN-02·08, FR-AL-ACT-05, FR-AL-OBS-03).

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("loop_runs",
                    sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("id", sa.String(64), primary_key=True),
                    sa.Column("goal", sa.Text(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("reason", sa.String(32), nullable=True), sa.Column("turns", sa.Integer(), nullable=False),
                    sa.Column("result", sa.JSON(), nullable=True), sa.Column("created_at", ts, nullable=False),
                    sa.Column("updated_at", ts, nullable=False))
    op.create_table("loop_checkpoints",
                    sa.Column("run_id", sa.String(64), primary_key=True), sa.Column("seq", sa.Integer(), primary_key=True),
                    sa.Column("tenant", sa.String(128), nullable=False), sa.Column("phase", sa.String(16), nullable=False),
                    sa.Column("state_blob", sa.Text(), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("loop_events",
                    sa.Column("run_id", sa.String(64), primary_key=True), sa.Column("seq", sa.Integer(), primary_key=True),
                    sa.Column("type", sa.String(32), nullable=False), sa.Column("data", sa.JSON(), nullable=False),
                    sa.Column("created_at", ts, nullable=False))
    op.create_table("loop_dedupe",
                    sa.Column("key", sa.String(64), primary_key=True), sa.Column("result", sa.JSON(), nullable=True),
                    sa.Column("created_at", ts, nullable=False))
    op.create_table("loop_artifacts",
                    sa.Column("run_id", sa.String(64), primary_key=True), sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("content", sa.Text(), nullable=False), sa.Column("created_at", ts, nullable=False))


def downgrade() -> None:
    for t in ("loop_artifacts", "loop_dedupe", "loop_events", "loop_checkpoints", "loop_runs"):
        op.drop_table(t)
