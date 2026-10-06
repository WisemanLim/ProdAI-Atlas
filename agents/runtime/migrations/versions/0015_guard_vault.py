"""GR-PII 가명 볼트 (FR-GR-PII-06).

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("guard_vault", sa.Column("tenant", sa.String(128), primary_key=True), sa.Column("session", sa.String(128), primary_key=True),
                    sa.Column("token", sa.String(64), primary_key=True), sa.Column("entity_type", sa.String(32), nullable=False),
                    sa.Column("value_hmac", sa.String(64), nullable=False), sa.Column("ciphertext", sa.Text(), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("guard_vault")
