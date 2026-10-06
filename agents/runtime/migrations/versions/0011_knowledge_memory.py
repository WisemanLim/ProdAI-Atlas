"""X-KB 지식베이스·원천·문서·청크·작업 큐·에이전트 메모리. PostgreSQL 은 pgvector(HNSW) + FTS(GIN) 열을 더한다.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table("kb_bases", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("name", sa.String(128), nullable=False), sa.Column("description", sa.Text(), nullable=False),
                    sa.Column("config", sa.JSON(), nullable=False), sa.Column("created_at", ts, nullable=False),
                    sa.UniqueConstraint("tenant", "name", name="uq_kb_tenant_name"))
    op.create_table("kb_sources", sa.Column("id", sa.String(36), primary_key=True), sa.Column("kb_id", sa.String(36), nullable=False),
                    sa.Column("type", sa.String(16), nullable=False), sa.Column("config", sa.JSON(), nullable=False),
                    sa.Column("cursor", sa.JSON(), nullable=False), sa.Column("interval_s", sa.Integer(), nullable=False),
                    sa.Column("last_sync_at", ts, nullable=True), sa.Column("created_at", ts, nullable=False))
    op.create_table("kb_documents", sa.Column("id", sa.String(36), primary_key=True), sa.Column("kb_id", sa.String(36), nullable=False),
                    sa.Column("source_id", sa.String(36), nullable=True), sa.Column("uri", sa.String(1024), nullable=False),
                    sa.Column("sha256", sa.String(64), nullable=False), sa.Column("title", sa.String(512), nullable=False),
                    sa.Column("acl_tags", sa.JSON(), nullable=False), sa.Column("subject_ids", sa.JSON(), nullable=False),
                    sa.Column("duplicate_of", sa.String(36), nullable=True), sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("chunks", sa.Integer(), nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_index("ix_kb_docs_kb_uri", "kb_documents", ["kb_id", "uri"])
    op.create_table("kb_chunks", sa.Column("id", sa.String(36), primary_key=True), sa.Column("kb_id", sa.String(36), nullable=False),
                    sa.Column("doc_id", sa.String(36), nullable=False), sa.Column("seq", sa.Integer(), nullable=False),
                    sa.Column("text", sa.Text(), nullable=False), sa.Column("embedding", sa.JSON(), nullable=False),
                    sa.Column("acl_tags", sa.JSON(), nullable=False), sa.Column("subject_ids", sa.JSON(), nullable=False),
                    sa.Column("source_uri", sa.String(1024), nullable=False))
    op.create_index("ix_kb_chunks_kb", "kb_chunks", ["kb_id"])
    op.create_index("ix_kb_chunks_doc", "kb_chunks", ["doc_id"])
    op.create_table("kb_jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("kind", sa.String(16), nullable=False),
                    sa.Column("kb_id", sa.String(36), nullable=True), sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("payload", sa.JSON(), nullable=False), sa.Column("result", sa.JSON(), nullable=True),
                    sa.Column("created_at", ts, nullable=False), sa.Column("finished_at", ts, nullable=True))
    op.create_table("kb_memories", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant", sa.String(128), nullable=False),
                    sa.Column("scope", sa.String(16), nullable=False), sa.Column("scope_id", sa.String(128), nullable=False),
                    sa.Column("type", sa.String(16), nullable=False), sa.Column("content", sa.Text(), nullable=False),
                    sa.Column("embedding", sa.JSON(), nullable=False), sa.Column("provenance", sa.JSON(), nullable=False),
                    sa.Column("confidence", sa.Float(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("reason", sa.Text(), nullable=True), sa.Column("subject_id", sa.String(128), nullable=True),
                    sa.Column("history", sa.JSON(), nullable=False), sa.Column("created_at", ts, nullable=False), sa.Column("updated_at", ts, nullable=False))
    op.create_index("ix_kb_mem_scope", "kb_memories", ["tenant", "scope", "scope_id", "type"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("ALTER TABLE kb_chunks ADD COLUMN embedding_v vector(256)")
        op.execute("ALTER TABLE kb_chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED")
        op.execute("CREATE INDEX ix_kb_chunks_hnsw ON kb_chunks USING hnsw (embedding_v vector_cosine_ops)")
        op.execute("CREATE INDEX ix_kb_chunks_tsv ON kb_chunks USING gin (tsv)")


def downgrade() -> None:
    for t in ("kb_memories", "kb_jobs", "kb_chunks", "kb_documents", "kb_sources", "kb_bases"):
        op.drop_table(t)
