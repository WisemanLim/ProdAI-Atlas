"""A2A Task 상태 머신 + SQL TaskStore (FR-A2A-SRV-04·06).

파티션 키는 (tenant, task_id) — 다른 tenant 의 Task 는 존재하지 않는 것처럼 보인다.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, Index, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

SUBMITTED, WORKING, INPUT_REQUIRED, AUTH_REQUIRED = (
    "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED")
COMPLETED, CANCELED, FAILED, REJECTED = (
    "TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED")
TERMINAL = frozenset({COMPLETED, CANCELED, FAILED, REJECTED})
STATES = frozenset({SUBMITTED, WORKING, INPUT_REQUIRED, AUTH_REQUIRED, *TERMINAL})

# 전이 검증 테이블 — 여기에 없는 전이는 버그로 간주해 거부한다.
TRANSITIONS: dict[str, frozenset[str]] = {
    SUBMITTED: frozenset({WORKING, CANCELED, REJECTED, FAILED}),
    WORKING: frozenset({COMPLETED, FAILED, CANCELED, INPUT_REQUIRED, AUTH_REQUIRED}),
    INPUT_REQUIRED: frozenset({WORKING, CANCELED, FAILED}),
    AUTH_REQUIRED: frozenset({WORKING, CANCELED, FAILED}),
}


class InvalidTransition(Exception):
    pass


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "a2a_tasks"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    context_id: Mapped[str] = mapped_column(String(64))
    skill: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(40))
    status_message: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    history: Mapped[list] = mapped_column(JSON)
    artifacts: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_a2a_tasks_tenant_context", "tenant", "context_id"),)


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:  # SQLite 는 tz 를 저장하지 않는다
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def to_wire(row: TaskRow, history_length: int | None = None) -> dict[str, Any]:
    history = row.history if history_length is None else row.history[-history_length:] if history_length else []
    status: dict[str, Any] = {"state": row.state, "timestamp": iso(row.updated_at)}
    if row.status_message:
        status["message"] = row.status_message
    return {"id": row.id, "contextId": row.context_id, "status": status, "artifacts": row.artifacts,
            "history": history, "metadata": {"skill": row.skill}}


class TaskStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, tenant: str, skill: str, message: dict[str, Any], context_id: str | None) -> TaskRow:
        ts = now()
        row = TaskRow(tenant=tenant, id=str(uuid.uuid4()), context_id=context_id or str(uuid.uuid4()), skill=skill,
                      state=SUBMITTED, status_message=None, history=[message], artifacts=[], created_at=ts, updated_at=ts)
        with Session(self.engine, expire_on_commit=False) as s, s.begin():
            s.add(row)
        return row

    def get(self, tenant: str, task_id: str) -> TaskRow | None:
        with Session(self.engine, expire_on_commit=False) as s:
            return s.get(TaskRow, (tenant, task_id))

    def list(self, tenant: str, context_id: str | None, state: str | None, page_size: int, offset: int) -> tuple[list[TaskRow], int]:
        q = select(TaskRow).where(TaskRow.tenant == tenant)
        if context_id:
            q = q.where(TaskRow.context_id == context_id)
        if state:
            q = q.where(TaskRow.state == state)
        with Session(self.engine, expire_on_commit=False) as s:
            total = len(s.scalars(q.with_only_columns(TaskRow.id)).all())
            rows = s.scalars(q.order_by(TaskRow.created_at.desc(), TaskRow.id).offset(offset).limit(page_size)).all()
            return list(rows), total

    def transition(self, tenant: str, task_id: str, to: str, *, message: dict[str, Any] | None = None,
                   artifacts: list[dict[str, Any]] | None = None, append_history: dict[str, Any] | None = None) -> TaskRow:
        with Session(self.engine, expire_on_commit=False) as s, s.begin():
            row = s.get(TaskRow, (tenant, task_id), with_for_update=True)
            if row is None:
                raise KeyError(task_id)
            if to not in TRANSITIONS.get(row.state, frozenset()):
                raise InvalidTransition(f"{row.state} → {to}")
            row.state, row.updated_at = to, now()
            row.status_message = message
            if artifacts is not None:
                row.artifacts = [*row.artifacts, *artifacts]
            extra = [m for m in (append_history, message) if m]
            if extra:
                row.history = [*row.history, *extra]
            s.flush()
            s.expunge(row)
            return row

