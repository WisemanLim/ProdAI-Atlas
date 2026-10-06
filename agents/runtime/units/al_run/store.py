"""AL-RUN 영속 계층 — 런·체크포인트(FR-AL-RUN-02)·이벤트(FR-AL-RUN-08)·멱등 결과(FR-AL-ACT-05)·잘린 원문(FR-AL-OBS-03).
SQLite(local)·PostgreSQL(prod) 공용 — 상태 blob 은 JSON → zlib → base64 TEXT."""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


class RunRow(Base):
    __tablename__ = "loop_runs"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))          # running | pending | success | truncated | aborted | blocked | failed
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CheckpointRow(Base):
    __tablename__ = "loop_checkpoints"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    phase: Mapped[str] = mapped_column(String(16))
    state_blob: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventRow(Base):
    __tablename__ = "loop_events"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DedupeRow(Base):
    __tablename__ = "loop_dedupe"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    result: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ArtifactRow(Base):
    __tablename__ = "loop_artifacts"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def pack(state: dict[str, Any]) -> str:
    return base64.b64encode(zlib.compress(json.dumps(state, ensure_ascii=False, default=str).encode())).decode()


def unpack(blob: str) -> dict[str, Any]:
    return json.loads(zlib.decompress(base64.b64decode(blob)))


class LoopStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def create_run(self, tenant: str, run_id: str, goal: str) -> None:
        with self.session() as s, s.begin():
            if s.get(RunRow, (tenant, run_id)) is None:
                s.add(RunRow(tenant=tenant, id=run_id, goal=goal, status="running", turns=0, created_at=now(), updated_at=now()))

    def finish_run(self, tenant: str, run_id: str, status: str, reason: str, turns: int, result: dict[str, Any]) -> None:
        with self.session() as s, s.begin():
            row = s.get(RunRow, (tenant, run_id))
            row.status, row.reason, row.turns, row.result, row.updated_at = status, reason, turns, result, now()

    def list_runs(self, tenant: str, limit: int, status: str | None = None) -> list[RunRow]:
        q = select(RunRow).where(RunRow.tenant == tenant).order_by(RunRow.updated_at.desc()).limit(limit)
        if status:
            q = q.where(RunRow.status == status)
        with self.session() as s:
            return list(s.scalars(q))

    def get_run(self, tenant: str, run_id: str) -> RunRow | None:
        with self.session() as s:
            return s.get(RunRow, (tenant, run_id))

    def checkpoint(self, tenant: str, run_id: str, phase: str, state: dict[str, Any]) -> int:
        with self.session() as s, s.begin():
            seq = (s.scalar(select(func.max(CheckpointRow.seq)).where(CheckpointRow.run_id == run_id)) or 0) + 1
            s.add(CheckpointRow(run_id=run_id, seq=seq, tenant=tenant, phase=phase, state_blob=pack(state), created_at=now()))
            row = s.get(RunRow, (tenant, run_id))
            if row:
                row.turns, row.updated_at = state.get("turns", 0), now()
            return seq

    def latest_checkpoint(self, tenant: str, run_id: str, seq: int | None = None) -> tuple[int, dict[str, Any]] | None:
        q = select(CheckpointRow).where(CheckpointRow.run_id == run_id, CheckpointRow.tenant == tenant)
        q = q.where(CheckpointRow.seq == seq) if seq else q.order_by(CheckpointRow.seq.desc()).limit(1)
        with self.session() as s:
            row = s.scalars(q).first()
            return (row.seq, unpack(row.state_blob)) if row else None

    def add_event(self, run_id: str, type_: str, data: dict[str, Any]) -> int:
        with self.session() as s, s.begin():
            seq = (s.scalar(select(func.max(EventRow.seq)).where(EventRow.run_id == run_id)) or 0) + 1
            s.add(EventRow(run_id=run_id, seq=seq, type=type_, data=data, created_at=now()))
            return seq

    def events(self, run_id: str, after: int = 0) -> list[EventRow]:
        with self.session() as s:
            return list(s.scalars(select(EventRow).where(EventRow.run_id == run_id, EventRow.seq > after).order_by(EventRow.seq)))

    def dedupe_get(self, key: str) -> Any | None:
        with self.session() as s:
            row = s.get(DedupeRow, key)
            return row.result if row else None

    def dedupe_put(self, key: str, result: Any) -> None:
        with self.session() as s, s.begin():
            if s.get(DedupeRow, key) is None:
                s.add(DedupeRow(key=key, result=result, created_at=now()))

    def put_artifact(self, run_id: str, content: str) -> str:
        key = hashlib.sha256(content.encode()).hexdigest()[:32]
        with self.session() as s, s.begin():
            if s.get(ArtifactRow, (run_id, key)) is None:
                s.add(ArtifactRow(run_id=run_id, key=key, content=content, created_at=now()))
        return f"atlas://runs/{run_id}/artifacts/{key}"

    def get_artifact(self, run_id: str, key: str) -> str | None:
        with self.session() as s:
            row = s.get(ArtifactRow, (run_id, key))
            return row.content if row else None
