"""EV-JUDGE 루브릭 judge 버전 · EV-SCORE 점수 · 온라인 judge 규칙.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("ev_judges", sa.Column("name", sa.String(128), primary_key=True), sa.Column("version", sa.Integer(), primary_key=True),
                    sa.Column("mode", sa.String(16), nullable=False), sa.Column("rubric_md", sa.Text(), nullable=False),
                    sa.Column("instructions", sa.Text(), nullable=False), sa.Column("model", sa.String(128), nullable=False),
                    sa.Column("jury", sa.JSON(), nullable=False), sa.Column("threshold", sa.Float(), nullable=False),
                    sa.Column("calibration", sa.JSON(), nullable=True), sa.Column("created_at", ts, nullable=False))
    op.create_table("ev_scores", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("source", sa.String(16), nullable=False), sa.Column("run_id", sa.String(36), nullable=True),
                    sa.Column("item_id", sa.String(36), nullable=True), sa.Column("trace_id", sa.String(64), nullable=True),
                    sa.Column("target", sa.String(64), nullable=True), sa.Column("scorer", sa.String(160), nullable=False),
                    sa.Column("judge_version", sa.Integer(), nullable=True), sa.Column("value", sa.Float(), nullable=False),
                    sa.Column("passed", sa.Boolean(), nullable=True), sa.Column("critique", sa.Text(), nullable=True),
                    sa.Column("payload", sa.JSON(), nullable=True), sa.Column("created_at", ts, nullable=False))
    op.create_index("ix_ev_scores_scorer", "ev_scores", ["scorer", "created_at"])
    op.create_index("ix_ev_scores_run", "ev_scores", ["run_id"])
    op.create_index("ix_ev_scores_trace", "ev_scores", ["trace_id"])
    op.create_table("ev_online_rules", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("name", sa.String(128), nullable=False), sa.Column("filter", sa.Text(), nullable=False),
                    sa.Column("sample_rate", sa.Float(), nullable=False), sa.Column("judge", sa.String(128), nullable=False),
                    sa.Column("dataset", sa.String(128), nullable=True), sa.Column("enabled", sa.Boolean(), nullable=False),
                    sa.Column("last_run_at", ts, nullable=True), sa.Column("created_at", ts, nullable=False))


def downgrade() -> None:
    op.drop_table("ev_online_rules")
    op.drop_index("ix_ev_scores_trace", "ev_scores")
    op.drop_index("ix_ev_scores_run", "ev_scores")
    op.drop_index("ix_ev_scores_scorer", "ev_scores")
    op.drop_table("ev_scores")
    op.drop_table("ev_judges")
