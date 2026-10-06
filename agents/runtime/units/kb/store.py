"""KB 저장소 (FR-X-KB-01~08) — 지식베이스·원천·문서·청크(벡터)·작업 큐·메모리·삭제 증명.

벡터는 이식 가능하게 JSON 으로 두고, PostgreSQL 에서는 마이그레이션이 pgvector 열(`embedding_v vector(256)`, HNSW)과
FTS 열(`tsv tsvector`, GIN)을 더한다 — 검색은 엔진별로 분기(search.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Engine,
    Float,
    Integer,
    String,
    Text,
    delete,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base

DIMS = 256


def now() -> datetime:
    return datetime.now(UTC)


def nid() -> str:
    return str(uuid.uuid4())


class KbRow(Base):
    __tablename__ = "kb_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON)          # chunk{strategy,size,overlap} · embed_model · acl_enforce
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceRow(Base):
    __tablename__ = "kb_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(16))       # upload · corpus · url
    config: Mapped[dict] = mapped_column(JSON)
    cursor: Mapped[dict] = mapped_column(JSON)          # {uri: sha256} 증분 동기화 커서
    interval_s: Mapped[int] = mapped_column(Integer)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocRow(Base):
    __tablename__ = "kb_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    uri: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512))
    acl_tags: Mapped[list] = mapped_column(JSON)
    subject_ids: Mapped[list] = mapped_column(JSON)
    duplicate_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16))     # queued · indexed · duplicate · error
    chunks: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChunkRow(Base):
    __tablename__ = "kb_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(36))
    doc_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON)
    acl_tags: Mapped[list] = mapped_column(JSON)
    subject_ids: Mapped[list] = mapped_column(JSON)
    source_uri: Mapped[str] = mapped_column(String(1024))


class JobRow(Base):
    __tablename__ = "kb_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))       # index · sync · consolidate · erasure
    kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16))     # queued · running · done · error
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryRow(Base):
    __tablename__ = "kb_memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(16))      # user · agent · session
    scope_id: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))       # working · episodic · semantic · procedural
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)      # trace_id · tool · user · confidence
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))     # active · quarantined · merged · deleted
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    history: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KbStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.pg = engine.dialect.name == "postgresql"

    def s(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def add(self, row: Any) -> Any:
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def get(self, cls: Any, key: str) -> Any:
        with self.s() as s:
            return s.get(cls, key)

    def update(self, cls: Any, key: str, **f: Any) -> Any:
        with self.s() as s, s.begin():
            row = s.get(cls, key)
            for k, v in f.items():
                setattr(row, k, v)
            return row

    def kb(self, tenant: str, key: str) -> KbRow | None:
        with self.s() as s:
            return s.get(KbRow, key) or s.scalars(select(KbRow).where(KbRow.tenant == tenant, KbRow.name == key)).first()

    def kbs(self, tenant: str | None = None) -> list[KbRow]:
        q = select(KbRow).order_by(KbRow.name)
        if tenant:
            q = q.where(KbRow.tenant == tenant)
        with self.s() as s:
            return list(s.scalars(q))

    def sources(self, kb_id: str | None = None) -> list[SourceRow]:
        q = select(SourceRow)
        if kb_id:
            q = q.where(SourceRow.kb_id == kb_id)
        with self.s() as s:
            return list(s.scalars(q))

    def docs(self, kb_id: str) -> list[DocRow]:
        with self.s() as s:
            return list(s.scalars(select(DocRow).where(DocRow.kb_id == kb_id).order_by(DocRow.uri)))

    def doc_by_uri(self, kb_id: str, uri: str) -> DocRow | None:
        with self.s() as s:
            return s.scalars(select(DocRow).where(DocRow.kb_id == kb_id, DocRow.uri == uri)).first()

    def doc_by_sha(self, kb_id: str, sha: str, exclude: str | None = None) -> DocRow | None:
        q = select(DocRow).where(DocRow.kb_id == kb_id, DocRow.sha256 == sha, DocRow.duplicate_of.is_(None))
        if exclude:
            q = q.where(DocRow.id != exclude)
        with self.s() as s:
            return s.scalars(q).first()

    def replace_chunks(self, doc: DocRow, chunks: list[dict[str, Any]]) -> None:
        with self.s() as s, s.begin():
            s.execute(delete(ChunkRow).where(ChunkRow.doc_id == doc.id))
            for i, ch in enumerate(chunks):
                cid = nid()
                s.add(ChunkRow(id=cid, kb_id=doc.kb_id, doc_id=doc.id, seq=i, text=ch["text"], embedding=ch["embedding"], acl_tags=doc.acl_tags,
                               subject_ids=doc.subject_ids, source_uri=doc.uri))
                ch["id"] = cid
            s.flush()
            if self.pg:   # pgvector 열 채우기 (tsv 는 생성 열)
                for ch in chunks:
                    s.execute(text("UPDATE kb_chunks SET embedding_v = CAST(:v AS vector) WHERE id = :id"), {"v": str(ch["embedding"]), "id": ch["id"]})

    def delete_doc(self, doc_id: str) -> None:
        with self.s() as s, s.begin():
            s.execute(delete(ChunkRow).where(ChunkRow.doc_id == doc_id))
            s.execute(delete(DocRow).where(DocRow.id == doc_id))

    def chunks(self, kb_id: str) -> list[ChunkRow]:
        with self.s() as s:
            return list(s.scalars(select(ChunkRow).where(ChunkRow.kb_id == kb_id)))

    def jobs_queued(self, limit: int = 5) -> list[JobRow]:
        with self.s() as s:
            return list(s.scalars(select(JobRow).where(JobRow.status == "queued").order_by(JobRow.created_at).limit(limit)))

    def memories(self, tenant: str, **f: Any) -> list[MemoryRow]:
        q = select(MemoryRow).where(MemoryRow.tenant == tenant)
        for k, v in f.items():
            if v is not None:
                q = q.where(getattr(MemoryRow, k) == v)
        with self.s() as s:
            return list(s.scalars(q.order_by(MemoryRow.created_at)))
