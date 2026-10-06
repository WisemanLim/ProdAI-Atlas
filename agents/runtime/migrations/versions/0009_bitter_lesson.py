"""BL-MODEL 후보 · BL-HARN 아티팩트(프롬프트·하네스·규칙)·라벨·보정 플래그 · BL 감사.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("bl_candidates", sa.Column("id", sa.String(36), primary_key=True), sa.Column("alias", sa.String(128), nullable=False),
                    sa.Column("base_model", sa.String(128), nullable=False), sa.Column("cand_model", sa.String(128), nullable=False),
                    sa.Column("provider", sa.String(64), nullable=False), sa.Column("status", sa.String(24), nullable=False),
                    sa.Column("decision", sa.String(32), nullable=True), sa.Column("alias_version_before", sa.Integer(), nullable=False),
                    sa.Column("detail", sa.JSON(), nullable=False), sa.Column("created_at", ts, nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_table("bl_artifacts", sa.Column("kind", sa.String(16), primary_key=True), sa.Column("name", sa.String(128), primary_key=True),
                    sa.Column("version", sa.Integer(), primary_key=True), sa.Column("body", sa.JSON(), nullable=False),
                    sa.Column("created_by", sa.String(128), nullable=False), sa.Column("created_at", ts, nullable=False))
    op.create_table("bl_labels", sa.Column("kind", sa.String(16), primary_key=True), sa.Column("name", sa.String(128), primary_key=True),
                    sa.Column("label", sa.String(32), primary_key=True), sa.Column("version", sa.Integer(), nullable=False),
                    sa.Column("updated_at", ts, nullable=False))
    op.create_table("bl_flags", sa.Column("key", sa.String(160), primary_key=True), sa.Column("enabled", sa.Boolean(), nullable=False),
                    sa.Column("reason", sa.Text(), nullable=False), sa.Column("introduced_for", sa.String(128), nullable=False),
                    sa.Column("removal_condition", sa.Text(), nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_table("bl_audit", sa.Column("id", sa.String(36), primary_key=True), sa.Column("ts", ts, nullable=False),
                    sa.Column("actor", sa.String(128), nullable=False), sa.Column("action", sa.String(64), nullable=False),
                    sa.Column("target", sa.String(256), nullable=False), sa.Column("detail", sa.JSON(), nullable=False))
    op.create_index("ix_bl_audit_target", "bl_audit", ["target", "ts"])


def downgrade() -> None:
    op.drop_index("ix_bl_audit_target", "bl_audit")
    for t in ("bl_audit", "bl_flags", "bl_labels", "bl_artifacts", "bl_candidates"):
        op.drop_table(t)
