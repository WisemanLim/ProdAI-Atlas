"""GR-IO 저장소 — 판정 로그(원문 미보관·해시만)·오탐 신고·예외·정책 번들 버전 (FR-GR-IO-08 · GR-POL-01)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)   # SQLite 는 tz 를 버린다


class VaultRow(Base):
    """가명 볼트 (FR-GR-PII-06): (tenant, session, token) → 원문 암호문(AES-GCM, guard-secret 파생 키). TTL 지나면 삭제."""
    __tablename__ = "guard_vault"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    session: Mapped[str] = mapped_column(String(128), primary_key=True)
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    value_hmac: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VerdictRow(Base):
    __tablename__ = "guard_verdicts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    boundary: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(20))
    content_sha256: Mapped[str] = mapped_column(String(64))
    categories: Mapped[list] = mapped_column(JSON)
    stages: Mapped[list] = mapped_column(JSON)
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FeedbackRow(Base):
    __tablename__ = "guard_feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    verdict_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExceptionRow(Base):
    __tablename__ = "guard_exceptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    checker: Mapped[str] = mapped_column(String(32))
    content_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyRow(Base):
    __tablename__ = "guard_policies"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    bundle: Mapped[dict] = mapped_column(JSON)
    publisher: Mapped[str] = mapped_column(String(128))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GuardStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def s(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def add_verdict(self, tenant: str, boundary: str, decision: str, sha: str, categories: list[str], stages: list[dict], version: str) -> str:
        vid = str(uuid.uuid4())
        with self.s() as s, s.begin():
            s.add(VerdictRow(id=vid, tenant=tenant, boundary=boundary, decision=decision, content_sha256=sha, categories=categories,
                             stages=stages, policy_version=version, created_at=now()))
        return vid

    def verdict(self, vid: str) -> VerdictRow | None:
        with self.s() as s:
            return s.get(VerdictRow, vid)

    def verdicts(self, tenant: str, limit: int) -> list[VerdictRow]:
        with self.s() as s:
            return list(s.scalars(select(VerdictRow).where(VerdictRow.tenant == tenant).order_by(VerdictRow.created_at.desc()).limit(limit)))

    def add_feedback(self, vid: str, kind: str, note: str | None) -> str:
        fid = str(uuid.uuid4())
        with self.s() as s, s.begin():
            s.add(FeedbackRow(id=fid, verdict_id=vid, kind=kind, note=note, created_at=now()))
        return fid

    def add_exception(self, tenant: str, checker: str, sha: str, ttl_days: int) -> str:
        eid = str(uuid.uuid4())
        with self.s() as s, s.begin():
            s.add(ExceptionRow(id=eid, tenant=tenant, checker=checker, content_sha256=sha, expires_at=now() + timedelta(days=ttl_days), created_at=now()))
        return eid

    def excepted(self, tenant: str, sha: str) -> set[str]:
        with self.s() as s:
            rows = s.scalars(select(ExceptionRow).where(ExceptionRow.tenant == tenant, ExceptionRow.content_sha256 == sha))
            return {r.checker for r in rows if aware(r.expires_at) > now()}

    def publish(self, tenant: str, bundle: dict[str, Any], publisher: str) -> bool:
        with self.s() as s, s.begin():
            if s.get(PolicyRow, (tenant, bundle["version"])) is not None:
                return False
            s.add(PolicyRow(tenant=tenant, version=bundle["version"], bundle=bundle, publisher=publisher, published_at=now()))
            return True

    def latest(self, tenant: str) -> PolicyRow | None:
        with self.s() as s:
            return s.scalars(select(PolicyRow).where(PolicyRow.tenant == tenant).order_by(PolicyRow.published_at.desc()).limit(1)).first()


def vault_load(engine: Engine, tenant: str, session: str) -> list[VaultRow]:
    with Session(engine, expire_on_commit=False) as s, s.begin():
        s.query(VaultRow).filter(VaultRow.expires_at < now()).delete()
        return list(s.scalars(select(VaultRow).where(VaultRow.tenant == tenant, VaultRow.session == session)))


def vault_put(engine: Engine, row: VaultRow) -> None:
    with Session(engine) as s, s.begin():
        s.merge(row)
