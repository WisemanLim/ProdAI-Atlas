"""OB 저장소 — SLO 정의 · 경보 규칙 · 경보 이벤트 (FR-OB-DASH-03·04)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, Float, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


class SloRow(Base):
    __tablename__ = "ob_slos"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))            # latency | error_rate
    objective: Mapped[float] = mapped_column(Float)          # 예: 0.99
    threshold_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_days: Mapped[int] = mapped_column(Integer)
    filter: Mapped[str] = mapped_column(String(1000))        # 검색 DSL (대상 span)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertRuleRow(Base):
    __tablename__ = "ob_alert_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    expr: Mapped[dict] = mapped_column(JSON)                 # {type: slo_burn|metric, ...}
    severity: Mapped[str] = mapped_column(String(16))
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    state: Mapped[str] = mapped_column(String(16))           # ok | firing
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertEventRow(Base):
    __tablename__ = "ob_alert_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))          # firing | resolved
    value: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    delivered: Mapped[str] = mapped_column(String(16))       # sent | failed | none
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def s(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def add_slo(self, **kw: Any) -> SloRow:
        row = SloRow(id=str(uuid.uuid4()), created_at=now(), **kw)
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def slo(self, sid: str) -> SloRow | None:
        with self.s() as s:
            return s.get(SloRow, sid)

    def slos(self) -> list[SloRow]:
        with self.s() as s:
            return list(s.scalars(select(SloRow).order_by(SloRow.created_at)))

    def add_rule(self, **kw: Any) -> AlertRuleRow:
        row = AlertRuleRow(id=str(uuid.uuid4()), state="ok", updated_at=now(), **kw)
        with self.s() as s, s.begin():
            s.add(row)
        return row

    def rules(self) -> list[AlertRuleRow]:
        with self.s() as s:
            return list(s.scalars(select(AlertRuleRow).order_by(AlertRuleRow.updated_at)))

    def set_rule_state(self, rid: str, state: str, value: float) -> None:
        with self.s() as s, s.begin():
            r = s.get(AlertRuleRow, rid)
            r.state, r.last_value, r.updated_at = state, value, now()

    def add_event(self, rule_id: str, status: str, value: float, payload: dict, delivered: str) -> None:
        with self.s() as s, s.begin():
            s.add(AlertEventRow(id=str(uuid.uuid4()), rule_id=rule_id, status=status, value=value, payload=payload,
                                delivered=delivered, created_at=now()))

    def events(self, rule_id: str | None = None) -> list[AlertEventRow]:
        q = select(AlertEventRow).order_by(AlertEventRow.created_at.desc()).limit(100)
        if rule_id:
            q = q.where(AlertEventRow.rule_id == rule_id)
        with self.s() as s:
            return list(s.scalars(q))
