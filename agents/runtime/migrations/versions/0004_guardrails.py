"""GR-IO 가드레일 — 판정·신고·예외·정책 번들 (FR-GR-IO-08 · GR-POL-01).

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("guard_verdicts",
                    sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("boundary", sa.String(16), nullable=False), sa.Column("decision", sa.String(20), nullable=False),
                    sa.Column("content_sha256", sa.String(64), nullable=False), sa.Column("categories", sa.JSON(), nullable=False),
                    sa.Column("stages", sa.JSON(), nullable=False), sa.Column("policy_version", sa.String(32), nullable=False),
                    sa.Column("created_at", ts, nullable=False))
    op.create_index("ix_guard_verdicts_tenant_created", "guard_verdicts", ["tenant", "created_at"])
    op.create_table("guard_feedback",
                    sa.Column("id", sa.String(36), primary_key=True), sa.Column("verdict_id", sa.String(36), nullable=False),
                    sa.Column("kind", sa.String(20), nullable=False), sa.Column("note", sa.Text(), nullable=True),
                    sa.Column("created_at", ts, nullable=False))
    op.create_table("guard_exceptions",
                    sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("checker", sa.String(32), nullable=False), sa.Column("content_sha256", sa.String(64), nullable=False),
                    sa.Column("expires_at", ts, nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_index("ix_guard_exceptions_lookup", "guard_exceptions", ["tenant", "content_sha256"])
    op.create_table("guard_policies",
                    sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("version", sa.String(32), primary_key=True),
                    sa.Column("bundle", sa.JSON(), nullable=False), sa.Column("publisher", sa.String(128), nullable=False),
                    sa.Column("published_at", ts, nullable=False))


def downgrade() -> None:
    for t in ("guard_policies", "guard_exceptions", "guard_feedback", "guard_verdicts"):
        op.drop_table(t)
