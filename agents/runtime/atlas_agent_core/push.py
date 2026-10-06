"""A2A Push notification (FR-A2A-SRV-03): Task 상태 변화를 호출자 webhook 으로 알린다.

발신 요청에는 에이전트 키(ES256)로 서명한 JWT(iss=에이전트 카드 URL, aud=webhook URL, task_id, jti, 5분)를 붙이고,
공개키는 에이전트의 /.well-known/jwks.json 에 둔다. 재시도: 지수 백오프, 최대 ATLAS_PUSH_MAX_RETRIES(기본 8).
SSRF: http(s) 만, 사설·루프백·링크로컬 주소는 ATLAS_PUSH_ALLOW_HOSTS 에 있는 호스트만 허용.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import os
import socket
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import JSON, DateTime, Engine, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core.tasks import Base

log = logging.getLogger("atlas.push")


class PushConfigRow(Base):
    __tablename__ = "a2a_push_configs"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(String(1024))
    token: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RemoteTaskRow(Base):
    __tablename__ = "a2a_remote_tasks"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    remote_task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(128))
    parent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    skill: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("webhook 은 http(s) URL 이어야 함")
    allow = {h.strip() for h in os.environ.get("ATLAS_PUSH_ALLOW_HOSTS", "").split(",") if h.strip()}
    if u.hostname in allow:
        return
    if u.scheme != "https":
        raise UnsafeURL("허용 목록 밖 webhook 은 https 만")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, u.port or 443)}
    except socket.gaierror as exc:
        raise UnsafeURL(f"호스트 해석 실패: {u.hostname}") from exc
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise UnsafeURL(f"사설·내부 주소로의 webhook 금지 ({u.hostname} → {a})")


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


class Signer:
    """에이전트 push 서명 키 — 같은 컨테이너의 워커들이 공유하도록 파일(0600)에 둔다."""

    def __init__(self, path: str | None = None):
        p = Path(path or os.environ.get("ATLAS_PUSH_KEY_FILE", "/tmp/atlas-push-key.pem"))
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            key = ec.generate_private_key(ec.SECP256R1())
            with os.fdopen(fd, "wb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        except FileExistsError:
            for _ in range(20):  # 다른 워커가 막 쓰는 중일 수 있다
                if p.stat().st_size > 0:
                    break
                time.sleep(0.05)
        self.key = serialization.load_pem_private_key(p.read_bytes(), None)
        nums = self.key.public_key().public_numbers()
        self.kid = base64.urlsafe_b64encode(nums.x.to_bytes(32, "big")[:9]).rstrip(b"=").decode()
        self.jwk = {"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": self.kid, "x": _b64(nums.x), "y": _b64(nums.y)}

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk]}

    def mint(self, iss: str, aud: str, task_id: str) -> str:
        now = int(time.time())
        return jwt.encode({"iss": iss, "aud": aud, "iat": now, "exp": now + 300, "jti": str(uuid.uuid4()), "task_id": task_id},
                          self.key, algorithm="ES256", headers={"kid": self.kid})


class PushStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def put(self, tenant: str, task_id: str, url: str, token: str | None, cid: str | None = None) -> PushConfigRow:
        check_url(url)
        row = PushConfigRow(tenant=tenant, task_id=task_id, id=cid or str(uuid.uuid4()), url=url, token=token, created_at=datetime.now(UTC))
        with Session(self.engine, expire_on_commit=False) as s, s.begin():
            s.merge(row)
        return row

    def list(self, tenant: str, task_id: str) -> list[PushConfigRow]:
        with Session(self.engine) as s:
            return list(s.scalars(select(PushConfigRow).where(PushConfigRow.tenant == tenant, PushConfigRow.task_id == task_id)))

    def delete(self, tenant: str, task_id: str, cid: str) -> int:
        with Session(self.engine) as s, s.begin():
            return s.execute(delete(PushConfigRow).where(PushConfigRow.tenant == tenant, PushConfigRow.task_id == task_id,
                                                         PushConfigRow.id == cid)).rowcount


def config_wire(r: PushConfigRow) -> dict[str, Any]:
    return {"taskId": r.task_id, "pushNotificationConfig": {"id": r.id, "url": r.url, "authentication": {"schemes": ["Bearer"]}}}


class Dispatcher:
    def __init__(self, store: PushStore, signer: Signer, issuer: str, transport: httpx.AsyncBaseTransport | None = None):
        self.store, self.signer, self.issuer, self.transport = store, signer, issuer, transport
        self.max_retries = int(os.environ.get("ATLAS_PUSH_MAX_RETRIES", "8"))
        self.base_delay = float(os.environ.get("ATLAS_PUSH_BASE_DELAY_S", "0.5"))
        self._jobs: set[asyncio.Task] = set()

    def notify(self, tenant: str, event: dict[str, Any], task_id: str) -> None:
        for cfg in self.store.list(tenant, task_id):
            job = asyncio.get_event_loop().create_task(self._send(cfg, event))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)

    async def _send(self, cfg: PushConfigRow, event: dict[str, Any]) -> bool:
        for attempt in range(self.max_retries + 1):
            headers = {"Authorization": f"Bearer {self.signer.mint(self.issuer, cfg.url, cfg.task_id)}", "Content-Type": "application/json"}
            if cfg.token:
                headers["X-A2A-Notification-Token"] = cfg.token
            try:
                async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as c:
                    res = await c.post(cfg.url, json=event, headers=headers)
                if res.status_code < 300:
                    return True
                if 400 <= res.status_code < 500 and res.status_code not in (408, 429):
                    log.warning("push rejected %s %s", cfg.url, res.status_code)
                    return False
            except httpx.HTTPError as exc:
                log.info("push retry %s (%s)", cfg.url, exc)
            await asyncio.sleep(self.base_delay * (2 ** attempt))
        log.warning("push gave up after %d retries: %s", self.max_retries, cfg.url)
        return False


async def verify_push(token: str, jwks_url: str, audience: str) -> dict[str, Any]:
    """수신 측(CLI-04 webhook): 발신 에이전트 JWKS 로 서명·aud·만료 확인."""
    client = jwt.PyJWKClient(jwks_url, cache_keys=True)
    key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
    return jwt.decode(token, key.key, algorithms=["ES256"], audience=audience, options={"require": ["exp", "iat", "jti"]})
