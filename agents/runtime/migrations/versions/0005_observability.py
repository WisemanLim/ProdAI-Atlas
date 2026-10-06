"""OB SLO·경보 (FR-OB-DASH-03·04).

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("ob_slos",
                    sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(128), nullable=False),
                    sa.Column("kind", sa.String(16), nullable=False), sa.Column("objective", sa.Float(), nullable=False),
                    sa.Column("threshold_ms", sa.Float(), nullable=True), sa.Column("window_days", sa.Integer(), nullable=False),
                    sa.Column("filter", sa.String(1000), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("ob_alert_rules",
                    sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(128), nullable=False),
                    sa.Column("expr", sa.JSON(), nullable=False), sa.Column("severity", sa.String(16), nullable=False),
                    sa.Column("webhook_url", sa.String(500), nullable=True), sa.Column("state", sa.String(16), nullable=False),
                    sa.Column("last_value", sa.Float(), nullable=True), sa.Column("updated_at", ts, nullable=False))
    op.create_table("ob_alert_events",
                    sa.Column("id", sa.String(36), primary_key=True), sa.Column("rule_id", sa.String(36), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False), sa.Column("value", sa.Float(), nullable=False),
                    sa.Column("payload", sa.JSON(), nullable=False), sa.Column("delivered", sa.String(16), nullable=False),
                    sa.Column("created_at", ts, nullable=False))


def downgrade() -> None:
    for t in ("ob_alert_events", "ob_alert_rules", "ob_slos"):
        op.drop_table(t)
