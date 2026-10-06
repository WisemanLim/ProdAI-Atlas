"""MAT 스냅샷 (FR-MAT-08) — 월 1회 자동 + 수동, 단계·점수·사실 이력."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base


class SnapshotRow(Base):
    __tablename__ = "mat_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org: Mapped[str] = mapped_column(String(128))
    project: Mapped[str] = mapped_column(String(128))
    level: Mapped[int] = mapped_column(Integer)
    scores: Mapped[dict] = mapped_column(JSON)
    facts: Mapped[dict] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(16))      # manual | monthly
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MatStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def add(self, org: str, project: str, level: int, scores: dict, facts: dict, kind: str) -> SnapshotRow:
        row = SnapshotRow(id=str(uuid.uuid4()), org=org, project=project, level=level, scores=scores, facts=facts, kind=kind, created_at=datetime.now(UTC))
        with Session(self.engine, expire_on_commit=False) as s, s.begin():
            s.add(row)
        return row

    def history(self, org: str, project: str) -> list[SnapshotRow]:
        with Session(self.engine) as s:
            return list(s.scalars(select(SnapshotRow).where(SnapshotRow.org == org, SnapshotRow.project == project).order_by(SnapshotRow.created_at)))

    def all_latest(self) -> list[SnapshotRow]:
        with Session(self.engine) as s:
            rows = list(s.scalars(select(SnapshotRow).order_by(SnapshotRow.created_at)))
        latest: dict[tuple[str, str], SnapshotRow] = {}
        for r in rows:
            latest[(r.org, r.project)] = r
        return list(latest.values())

    @staticmethod
    def view(r: SnapshotRow) -> dict[str, Any]:
        at = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)   # SQLite 는 tz 를 버린다
        return {"id": r.id, "org": r.org, "project": r.project, "level": r.level, "scores": r.scores, "facts": r.facts, "kind": r.kind,
                "created_at": at.isoformat()}
