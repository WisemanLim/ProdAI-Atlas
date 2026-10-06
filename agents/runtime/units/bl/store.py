"""BL 저장소 — 모델 후보·버전 아티팩트(프롬프트·하네스·규칙)·라벨·보정 플래그·감사 (FR-BL-*)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Engine,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


class CandidateRow(Base):
    __tablename__ = "bl_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alias: Mapped[str] = mapped_column(String(128))
    base_model: Mapped[str] = mapped_column(String(128))
    cand_model: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))     # registered · evaluating · decided · canary · promoted · rolled_back · error
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)   # promote · review-cost · hold · insufficient-evidence
    alias_version_before: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON)          # suites · metrics · canary · reasons · history
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ArtifactRow(Base):
    """버전 아티팩트 — kind = prompt | harness | rules. (kind, name, version) 불변."""
    __tablename__ = "bl_artifacts"
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LabelRow(Base):
    __tablename__ = "bl_labels"
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    label: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FlagRow(Base):
    __tablename__ = "bl_flags"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    introduced_for: Mapped[str] = mapped_column(String(128))
    removal_condition: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditRow(Base):
    __tablename__ = "bl_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict] = mapped_column(JSON)


class BlStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def s(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # 후보
    def add_candidate(self, **f: Any) -> CandidateRow:
        row = CandidateRow(id=str(uuid.uuid4()), created_at=now(), updated_at=now(), **f)
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def candidate(self, cid: str) -> CandidateRow | None:
        with self.s() as s:
            return s.get(CandidateRow, cid)

    def candidates(self) -> list[CandidateRow]:
        with self.s() as s:
            return list(s.scalars(select(CandidateRow).order_by(CandidateRow.created_at.desc())))

    def update_candidate(self, cid: str, **f: Any) -> CandidateRow:
        with self.s() as s, s.begin():
            row = s.get(CandidateRow, cid)
            for k, v in f.items():
                setattr(row, k, v)
            row.updated_at = now()
            return row

    # 아티팩트·라벨
    def add_artifact(self, kind: str, name: str, body: dict, by: str) -> ArtifactRow:
        with self.s() as s, s.begin():
            v = (s.scalar(select(func.max(ArtifactRow.version)).where(ArtifactRow.kind == kind, ArtifactRow.name == name)) or 0) + 1
            row = ArtifactRow(kind=kind, name=name, version=v, body=body, created_by=by, created_at=now())
            s.add(row)
            return row

    def artifact(self, kind: str, name: str, version: int | None = None, label: str | None = None) -> ArtifactRow | None:
        with self.s() as s:
            if label:
                lb = s.get(LabelRow, (kind, name, label))
                if lb is None:
                    return None
                version = lb.version
            q = select(ArtifactRow).where(ArtifactRow.kind == kind, ArtifactRow.name == name)
            q = q.where(ArtifactRow.version == version) if version else q.order_by(ArtifactRow.version.desc()).limit(1)
            return s.scalars(q).first()

    def artifacts(self, kind: str) -> list[ArtifactRow]:
        with self.s() as s:
            return list(s.scalars(select(ArtifactRow).where(ArtifactRow.kind == kind).order_by(ArtifactRow.name, ArtifactRow.version)))

    def labels(self, kind: str, name: str) -> dict[str, int]:
        with self.s() as s:
            return {r.label: r.version for r in s.scalars(select(LabelRow).where(LabelRow.kind == kind, LabelRow.name == name))}

    def set_label(self, kind: str, name: str, label: str, version: int) -> None:
        with self.s() as s, s.begin():
            row = s.get(LabelRow, (kind, name, label))
            if row is None:
                s.add(LabelRow(kind=kind, name=name, label=label, version=version, updated_at=now()))
            else:
                row.version, row.updated_at = version, now()

    # 플래그
    def put_flag(self, **f: Any) -> FlagRow:
        with self.s() as s, s.begin():
            row = s.get(FlagRow, f["key"])
            if row is None:
                row = FlagRow(updated_at=now(), **f)
                s.add(row)
            else:
                for k, v in f.items():
                    setattr(row, k, v)
                row.updated_at = now()
            return row

    def flags(self) -> list[FlagRow]:
        with self.s() as s:
            return list(s.scalars(select(FlagRow).order_by(FlagRow.key)))

    # 감사
    def audit(self, actor: str, action: str, target: str, detail: dict | None = None) -> None:
        with self.s() as s, s.begin():
            s.add(AuditRow(id=str(uuid.uuid4()), ts=now(), actor=actor, action=action, target=target, detail=detail or {}))

    def audits(self, target: str | None = None, limit: int = 200) -> list[AuditRow]:
        q = select(AuditRow).order_by(AuditRow.ts.desc()).limit(limit)
        if target:
            q = q.where(AuditRow.target.like(f"{target}%"))
        with self.s() as s:
            return list(s.scalars(q))
