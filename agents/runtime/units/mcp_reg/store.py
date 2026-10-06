"""MCP-REG 저장소 — mcp_servers(name, version) · mcp_allowlists(tenant)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


class ServerRow(Base):
    __tablename__ = "mcp_servers"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))        # scanned | rejected | approved | revoked
    trust: Mapped[str] = mapped_column(String(16))         # unverified | community | verified | official
    scan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pins: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # tool → sha256(name+description+schema)
    protocol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AllowlistRow(Base):
    __tablename__ = "mcp_allowlists"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HealthRow(Base):
    __tablename__ = "mcp_health"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))         # closed · open · half_open
    failures: Mapped[int] = mapped_column(Integer)
    checks: Mapped[int] = mapped_column(Integer)
    errors: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelRow(Base):
    """버전 채널 포인터 (FR-MCP-REG-07): (서버, dev|staging|prod) → 승인 버전. 롤백 = 이전 포인터로."""
    __tablename__ = "mcp_channels"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    history: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KeyRow(Base):
    """게시자 서명 키 (FR-MCP-REG-04)."""
    __tablename__ = "mcp_keys"
    kid: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_key: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RegistryStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def s(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def upsert(self, name: str, version: str, **fields: Any) -> ServerRow:
        with self.s() as s, s.begin():
            row = s.get(ServerRow, (name, version))
            if row is None:
                row = ServerRow(name=name, version=version, created_at=now(), trust="unverified", status="scanned", server_json={})
                s.add(row)
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = now()
            return row

    def channel(self, name: str, channel: str) -> ChannelRow | None:
        with self.s() as s:
            return s.get(ChannelRow, (name, channel))

    def channels(self, name: str) -> list[ChannelRow]:
        with self.s() as s:
            return list(s.scalars(select(ChannelRow).where(ChannelRow.name == name)))

    def set_channel(self, name: str, channel: str, version: str, by: str) -> ChannelRow:
        with self.s() as s, s.begin():
            row = s.get(ChannelRow, (name, channel))
            prev = row.version if row else None
            if row is None:
                row = ChannelRow(name=name, channel=channel, version=version, history=[], updated_at=now())
                s.add(row)
            row.history = [*row.history, {"from": prev, "to": version, "by": by, "at": now().isoformat()}]
            row.version, row.updated_at = version, now()
            return row

    def keys(self) -> dict[str, str]:
        with self.s() as s:
            return {k.kid: k.public_key for k in s.scalars(select(KeyRow))}

    def put_key(self, kid: str, pem: str, owner: str) -> None:
        with self.s() as s, s.begin():
            s.merge(KeyRow(kid=kid, public_key=pem, owner=owner, created_at=now()))

    def get(self, name: str, version: str) -> ServerRow | None:
        with self.s() as s:
            return s.get(ServerRow, (name, version))

    def list(self, search: str | None = None, name: str | None = None) -> list[ServerRow]:
        q = select(ServerRow).order_by(ServerRow.name, ServerRow.version)
        if name:
            q = q.where(ServerRow.name == name)
        with self.s() as s:
            rows = list(s.scalars(q))
        if search:
            rows = [r for r in rows if search.lower() in (r.name + " " + str(r.server_json.get("description", ""))).lower()]
        return rows

    def set_allowlist(self, tenant: str, policy: dict[str, Any]) -> None:
        with self.s() as s, s.begin():
            row = s.get(AllowlistRow, tenant)
            if row is None:
                s.add(AllowlistRow(tenant=tenant, policy=policy, updated_at=now()))
            else:
                row.policy, row.updated_at = policy, now()

    def allowlist(self, tenant: str) -> dict[str, Any] | None:
        with self.s() as s:
            row = s.get(AllowlistRow, tenant)
            return row.policy if row else None

    def health(self, name: str, version: str) -> dict | None:
        with Session(self.engine) as s:
            h = s.get(HealthRow, (name, version))
            if h is None:
                return None
            return {"state": h.state, "failures": h.failures, "checks": h.checks, "errors": h.errors, "error_rate": round(h.errors / max(1, h.checks), 4),
                    "latency_ms": h.latency_ms, "last_error": h.last_error, "checked_at": h.checked_at.isoformat()}

    def record_health(self, name: str, version: str, ok: bool, ms: int, err: str | None, open_after: int) -> dict:
        """연속 실패 open_after 번이면 open, 성공하면 closed (open 중 성공 = 복구)."""
        with Session(self.engine) as s, s.begin():
            h = s.get(HealthRow, (name, version))
            if h is None:
                h = HealthRow(name=name, version=version, state="closed", failures=0, checks=0, errors=0, checked_at=datetime.now(UTC))
                s.add(h)
            h.checks += 1
            h.latency_ms, h.checked_at = ms, datetime.now(UTC)
            if ok:
                h.failures, h.state, h.last_error = 0, "closed", None
            else:
                h.failures += 1
                h.errors += 1
                h.last_error = err
                if h.failures >= open_after:
                    h.state = "open"
        return self.health(name, version)
