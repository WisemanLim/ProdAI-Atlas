"""X-RPT 저장소 — 사용량 이벤트·통합 감사(해시 체인, 추가 전용)·요율표·인보이스·SIEM 싱크·보고서·이상 탐지."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


def nid() -> str:
    return str(uuid.uuid4())


class UsageRow(Base):
    __tablename__ = "rpt_usage"
    __table_args__ = (UniqueConstraint("source", "source_id", "meter", name="uq_rpt_usage_src"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tenant: Mapped[str] = mapped_column(String(128))
    project: Mapped[str] = mapped_column(String(128), default="default")
    unit_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meter: Mapped[str] = mapped_column(String(32))          # requests · tokens_in · tokens_out · tokens_cached · tool_calls · sbx_cpu_s · storage_gb_h · tasks
    qty: Mapped[float] = mapped_column(Float)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32))          # gateway · sandbox · api
    source_id: Mapped[str] = mapped_column(String(128))


class AuditRow(Base):
    __tablename__ = "rpt_audit"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[str] = mapped_column(String(36), unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tenant: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(256))
    actor_type: Mapped[str] = mapped_column(String(16))      # user · agent · service · system
    action: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(String(256))
    result: Mapped[str] = mapped_column(String(32), default="success")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32))          # manager · event
    source_id: Mapped[str] = mapped_column(String(128), unique=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class RateRow(Base):
    __tablename__ = "rpt_rates"
    plan: Mapped[str] = mapped_column(String(64), primary_key=True)
    meter: Mapped[str] = mapped_column(String(32), primary_key=True)
    unit_price: Mapped[float] = mapped_column(Float)          # 통화 단위 / meter 단위 (cost 미터는 USD 당 배수)
    markup_pct: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")


class PlanRow(Base):
    __tablename__ = "rpt_plans"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    project: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan: Mapped[str] = mapped_column(String(64))
    department: Mapped[str] = mapped_column(String(128), default="")


class InvoiceRow(Base):
    __tablename__ = "rpt_invoices"
    __table_args__ = (UniqueConstraint("month", "tenant", "project", name="uq_rpt_invoice"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nid)
    month: Mapped[str] = mapped_column(String(7))
    tenant: Mapped[str] = mapped_column(String(128))
    project: Mapped[str] = mapped_column(String(128))
    plan: Mapped[str] = mapped_column(String(64))
    department: Mapped[str] = mapped_column(String(128), default="")
    lines: Mapped[list] = mapped_column(JSON)
    subtotal: Mapped[float] = mapped_column(Float)
    markup: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SinkRow(Base):
    __tablename__ = "rpt_siem_sinks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(32))            # splunk_hec · http
    url: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16))          # ocsf · otel
    secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filter: Mapped[list] = mapped_column(JSON, default=list) # action 접두어 (빈 목록 = 전부)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0)  # 마지막으로 보낸 감사 seq — 실패하면 그대로(= 버퍼)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportRow(Base):
    __tablename__ = "rpt_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nid)
    month: Mapped[str] = mapped_column(String(7))
    tenant: Mapped[str] = mapped_column(String(128))
    project: Mapped[str] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSON)
    html: Mapped[str] = mapped_column(Text)
    sent_to: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AnomalyRow(Base):
    __tablename__ = "rpt_anomalies"
    __table_args__ = (UniqueConstraint("tenant", "agent_id", "meter", "window_start", name="uq_rpt_anomaly"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nid)
    tenant: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(128))
    meter: Mapped[str] = mapped_column(String(32))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float] = mapped_column(Float)
    mean: Mapped[float] = mapped_column(Float)
    std: Mapped[float] = mapped_column(Float)
    z: Mapped[float] = mapped_column(Float)
    traces: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
