"""EV 골든셋·회귀 (FR-EV-GOLD-01·02·06 · EV-REG).

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("ev_datasets", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(128), nullable=False, unique=True),
                    sa.Column("description", sa.Text(), nullable=False), sa.Column("input_schema", sa.JSON(), nullable=True),
                    sa.Column("created_at", ts, nullable=False))
    op.create_table("ev_items", sa.Column("id", sa.String(36), primary_key=True), sa.Column("dataset_id", sa.String(36), nullable=False),
                    sa.Column("input", sa.JSON(), nullable=False), sa.Column("expected_output", sa.JSON(), nullable=True),
                    sa.Column("rubric", sa.JSON(), nullable=True), sa.Column("tags", sa.JSON(), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False), sa.Column("source", sa.String(32), nullable=False),
                    sa.Column("source_trace_id", sa.String(64), nullable=True), sa.Column("content_sha256", sa.String(64), nullable=False),
                    sa.Column("created_at", ts, nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_index("ix_ev_items_dataset_status", "ev_items", ["dataset_id", "status"])
    op.create_table("ev_item_labels", sa.Column("id", sa.String(36), primary_key=True), sa.Column("item_id", sa.String(36), nullable=False),
                    sa.Column("from_status", sa.String(16), nullable=False), sa.Column("to_status", sa.String(16), nullable=False),
                    sa.Column("verdict", sa.String(8), nullable=True), sa.Column("critique", sa.Text(), nullable=True),
                    sa.Column("actor", sa.String(64), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("ev_dataset_versions", sa.Column("dataset_id", sa.String(36), primary_key=True), sa.Column("version", sa.Integer(), primary_key=True),
                    sa.Column("manifest", sa.JSON(), nullable=False), sa.Column("items", sa.JSON(), nullable=False),
                    sa.Column("manifest_sha256", sa.String(64), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("ev_runs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("suite_name", sa.String(128), nullable=False),
                    sa.Column("suite", sa.JSON(), nullable=False), sa.Column("dataset_id", sa.String(36), nullable=False),
                    sa.Column("dataset_version", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("gate", sa.String(8), nullable=True), sa.Column("summary", sa.JSON(), nullable=True),
                    sa.Column("created_at", ts, nullable=False), sa.Column("finished_at", ts, nullable=True))
    op.create_table("ev_results", sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), nullable=False),
                    sa.Column("item_id", sa.String(36), nullable=False), sa.Column("target", sa.String(64), nullable=False),
                    sa.Column("epoch", sa.Integer(), nullable=False), sa.Column("passed", sa.Boolean(), nullable=False),
                    sa.Column("scores", sa.JSON(), nullable=False), sa.Column("output", sa.Text(), nullable=True),
                    sa.Column("latency_ms", sa.Float(), nullable=False), sa.Column("cost_usd", sa.Float(), nullable=False),
                    sa.Column("error", sa.Text(), nullable=True))
    op.create_index("ix_ev_results_run", "ev_results", ["run_id"])


def downgrade() -> None:
    for t in ("ev_results", "ev_runs", "ev_dataset_versions", "ev_item_labels", "ev_items", "ev_datasets"):
        op.drop_table(t)
