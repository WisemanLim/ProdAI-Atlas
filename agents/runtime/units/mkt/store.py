"""X-MKT 저장소 — 템플릿(버전·가시성·게시 상태·서명 산출물)·설치 기록·신뢰 키·MCP 공개 카탈로그 사본."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from atlas_agent_core.tasks import Base


def now() -> datetime:
    return datetime.now(UTC)


def nid() -> str:
    return str(uuid.uuid4())


class TemplateRow(Base):
    __tablename__ = "mkt_templates"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_mkt_templates_nv"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nid)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    unit_ids: Mapped[list] = mapped_column(JSON, default=list)
    layer: Mapped[str] = mapped_column(String(16), default="")
    visibility: Mapped[str] = mapped_column(String(16), default="public")      # private · org · public
    tenant: Mapped[str] = mapped_column(String(128), default="default")
    project: Mapped[str] = mapped_column(String(128), default="default")
    publisher: Mapped[str] = mapped_column(String(128), default="platform")
    status: Mapped[str] = mapped_column(String(16), default="draft")            # draft · pending · published · rejected · deprecated
    replaced_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    manifest: Mapped[dict] = mapped_column(JSON)
    files: Mapped[dict] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(String(64))
    signature: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sbom: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    scan: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InstallRow(Base):
    __tablename__ = "mkt_installs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nid)
    template: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(64))
    tenant: Mapped[str] = mapped_column(String(128))
    project: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict] = mapped_column(JSON)                                  # 비밀 파라미터는 Manager 비밀 참조로만
    status: Mapped[str] = mapped_column(String(16))                             # installed · failed
    target: Mapped[dict] = mapped_column(JSON, default=dict)                    # agent_id · deployment_id · mcp_server · artifact
    notified_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KeyRow(Base):
    __tablename__ = "mkt_keys"
    kid: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_key: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16))                             # platform · publisher
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class McpCatalogRow(Base):
    __tablename__ = "mkt_mcp_catalog"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="available")       # available · imported · rejected
    review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
