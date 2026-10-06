"""MCP-REG MCP Server Registry — 사설 레지스트리(공식 Registry API v0.1 호환)·허용목록·도구 고정·설치 전 스캔
(FR-MCP-REG-01·03·05·06). 서명 검증(REG-04)은 후속.

읽기: GET /v0.1/servers · /v0.1/servers/{name}/versions[/{version}] · /v0.1/tenants/{tenant}/effective
관리(Bearer ATLAS_MCP_REG_ADMIN_TOKEN): POST /admin/servers · POST /admin/servers/{name}/versions/{version}/approve|revoke · PUT /admin/allowlist/{tenant}
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastmcp import Client
from pydantic import BaseModel, Field

from atlas_agent_core import AgentSpec, Skill, TaskContext, events
from units.mcp_reg.scan import scan
from units.mcp_reg.store import RegistryStore, ServerRow

MANIFEST = Path(__file__).with_name("atlas-agent.json")
TRUSTED = ("verified", "official")
META = "io.prodai.atlas/registry"


def tool_hash(t: dict[str, Any]) -> str:
    """도구 정의 지문 — 이름·설명·입력 스키마 (FR-MCP-REG-05)."""
    body = json.dumps({"name": t["name"], "description": t.get("description") or "", "input_schema": t.get("input_schema") or {}},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


@contextlib.asynccontextmanager
async def client_for(url: str, timeout: float = 15):
    """일반 연결을 먼저 시도하고, 실패 시 401(OAuth 보호)이면 서비스 계정 토큰을 붙여 재연결 (FR-MCP-HOST-03 — 등록 스캔·헬스)."""
    from fastmcp.client.transports import StreamableHttpTransport

    from units.mcp_host import oauth
    c = Client(url, timeout=timeout)
    try:
        await c.__aenter__()
    except Exception:
        www = await oauth.probe(url) if isinstance(url, str) and url.startswith("http") else None
        if www is None:
            raise
        tok = await oauth.token(url, "registry", www)
        c = Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {tok['access_token']}"}), timeout=timeout)
        await c.__aenter__()
    try:
        yield c
    finally:
        await c.__aexit__(None, None, None)


async def fetch_tools(url: str) -> tuple[list[dict[str, Any]], str | None]:
    """원격 MCP 서버의 tools/list 와 협상된 프로토콜 버전."""
    async with client_for(url) as c:
        tools = await c.list_tools()
        return [{"name": t.name, "title": t.title, "description": t.description, "input_schema": t.input_schema,
                 "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else {}} for t in tools], c.protocol_version


def remote_url(server_json: dict[str, Any]) -> str:
    for r in server_json.get("remotes", []):
        if r.get("type") in ("streamable-http", "http") and r.get("url"):
            return r["url"]
    # stdio 패키지 = 저신뢰 서드파티 → 격리 브리지(mcp-sbx)가 실행·중계 (FR-X-SBX-07 · MCP-SYS-03)
    for p in server_json.get("packages", []):
        if (p.get("transport") or {}).get("type") == "stdio":
            base = os.environ.get("ATLAS_MCP_SBX_URL", "http://mcp-sbx:8000").rstrip("/")
            return f"{base}/servers/{server_json['name'].replace('/', '--')}/mcp"
    raise HTTPException(422, "remotes[] 에 streamable-http url 또는 packages[] 에 stdio 패키지 필요")


def entry(row: ServerRow) -> dict[str, Any]:
    return {"server": row.server_json, "_meta": {META: {"status": row.status, "trust": row.trust, "protocol": row.protocol,
                                                        "scanScore": (row.scan or {}).get("score"), "updatedAt": row.updated_at.isoformat(),
                                                        "verification": (row.scan or {}).get("verification"), "registryMeta": registry_meta(row.server_json)}}}


def store() -> RegistryStore:
    return RegistryStore(agent.resources["engine"])


def require_admin(authorization: str | None) -> None:
    token = os.environ.get("ATLAS_MCP_REG_ADMIN_TOKEN", "")
    if not token or token == "CHANGE_ME" or authorization != f"Bearer {token}":
        raise HTTPException(401, "관리 토큰 필요")


class ServerJSON(BaseModel):
    model_config = {"populate_by_name": True}
    name: str = Field(pattern=r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$", description="역DNS 이름, 예: io.prodai/demo")
    version: str = Field(min_length=1, max_length=64)
    description: str = ""
    remotes: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    meta: dict[str, Any] = Field(default_factory=dict, alias="_meta")   # 서명·출처·레지스트리 메타 (REG-04·08)


SIG_KEY, PROV_KEY, META_KEY = "io.prodai.atlas/signature", "io.prodai.atlas/provenance", "io.prodai.atlas/registry-meta"
COPYLEFT = ("AGPL", "SSPL", "BUSL", "GPL-3", "EUPL")


def server_digest(sj: dict[str, Any]) -> str:
    """서명 대상 = 정규화한 server.json(기본값 채움, _meta 제외) 의 sha256 — 서명자·레지스트리가 같은 바이트를 본다."""
    body = {"name": sj.get("name"), "version": sj.get("version"), "description": sj.get("description") or "",
            "remotes": sj.get("remotes") or [], "packages": sj.get("packages") or []}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def signed_message(digest: str) -> bytes:
    return f"atlas-mcp-server/v1|sha256:{digest}".encode()


def verify_server(sj: dict[str, Any]) -> dict[str, Any]:
    """게시자 서명(ed25519)·SLSA provenance(subject digest) 확인 (FR-MCP-REG-04). 결과 = {signature, provenance, digest}."""
    from units.mkt import signing  # ed25519 PEM 도구 재사용
    meta = sj.get("_meta") or {}
    digest = server_digest(sj)
    sig, prov = meta.get(SIG_KEY), meta.get(PROV_KEY)
    out = {"digest": digest, "signature": "missing", "provenance": "missing", "packages": bool(sj.get("packages"))}
    if sig:
        pem = store().keys().get(sig.get("kid", ""))
        if pem is None:
            out["signature"] = "untrusted_key"
        else:
            import base64

            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import serialization
            try:
                serialization.load_pem_public_key(pem.encode()).verify(base64.b64decode(sig.get("sig", "")), signed_message(digest))  # type: ignore[union-attr]
                out["signature"] = "valid"
            except (InvalidSignature, ValueError):
                out["signature"] = "invalid"
        out["kid"] = sig.get("kid")
    if prov:
        subj = ((prov.get("subject") or [{}])[0].get("digest") or {}).get("sha256")
        out["provenance"] = "valid" if subj == digest and prov.get("predicateType") == signing.PROVENANCE_TYPE else "subject_mismatch"
    return out


def registry_meta(sj: dict[str, Any]) -> dict[str, Any]:
    """신뢰 등급 외 메타 (FR-MCP-REG-08): owners·dataClass·requiredScopes·license·auth·transport + 라이선스 경고."""
    m = dict((sj.get("_meta") or {}).get(META_KEY) or {})
    m.setdefault("transport", "stdio" if sj.get("packages") and not sj.get("remotes") else "streamable-http")
    lic = str(m.get("license") or "")
    m["warnings"] = [f"copyleft 라이선스 {lic} — 결합 방식 검토"] if any(x in lic.upper() for x in COPYLEFT) else []
    return m


class Approve(BaseModel):
    trust: str = Field("community", pattern="^(unverified|community|verified|official)$")


class Allowlist(BaseModel):
    servers: list[dict[str, str]] = []                       # [{name, version(glob)}]
    tools: dict[str, list[str]] = {"include": ["*"], "exclude": []}


router = APIRouter()
BLOCK_SCORE = int(os.environ.get("ATLAS_MCP_REG_BLOCK_SCORE", "70"))


@router.get("/v0.1/servers")
async def list_servers(search: str | None = None, limit: int = 50, cursor: int = 0):
    rows = store().list(search)
    page = rows[cursor:cursor + limit]
    nxt = cursor + limit if cursor + limit < len(rows) else None
    return {"servers": [entry(r) for r in page], "metadata": {"count": len(page), "nextCursor": nxt}}


@router.get("/v0.1/servers/{name:path}/versions/{version}")
async def get_version(name: str, version: str):
    row = store().get(name, version)
    if row is None:
        raise HTTPException(404, "server version not found")
    return {**entry(row), "pins": row.pins or {}, "scan": row.scan}


@router.get("/v0.1/servers/{name:path}/versions")
async def list_versions(name: str):
    rows = store().list(name=name)
    if not rows:
        raise HTTPException(404, "server not found")
    return {"servers": [entry(r) for r in rows]}


@router.post("/admin/servers", status_code=201)
async def register(body: ServerJSON, authorization: str | None = Header(None)):
    """등록 = 설치 전 스캔. 점수가 기준 이상이면 rejected (FR-MCP-REG-06)."""
    require_admin(authorization)
    sj = body.model_dump(by_alias=True)
    if not sj.get("_meta"):
        sj.pop("_meta", None)
    tools, proto = await fetch_tools(remote_url(sj))
    result = scan(tools)
    result["verification"] = verify_server(sj)
    if result["verification"]["signature"] == "invalid":
        await events.publish(agent, "plt.mcp.reg.verify_failed", {"server": body.name, "version": body.version, "reason": "invalid_signature"})
    status = "rejected" if result["score"] >= BLOCK_SCORE else "scanned"
    row = store().upsert(body.name, body.version, server_json=sj, status=status, scan=result, protocol=proto, pins=None)
    return entry(row) | {"scan": result}


@router.post("/admin/servers/{name:path}/versions/{version}/approve")
async def approve(name: str, version: str, body: Approve, authorization: str | None = Header(None)):
    """승인 시점의 도구 정의를 고정한다. 이후 정의가 바뀌면 MCP-HOST 가 해당 도구를 차단 (rug pull, FR-MCP-REG-05)."""
    require_admin(authorization)
    row = store().get(name, version)
    if row is None:
        raise HTTPException(404, "server version not found")
    if row.status == "rejected":
        raise HTTPException(409, f"보안 스캔 거부 (score {row.scan.get('score')}) — 재등록 필요")
    v = verify_server(row.server_json)
    if v["signature"] == "invalid" or (body.trust in TRUSTED and v["packages"] and v["signature"] != "valid"):
        await events.publish(agent, "plt.mcp.reg.verify_failed", {"server": name, "version": version, "trust": body.trust, "verification": v})
        raise HTTPException(409, {"error": "verify_failed", "message": f"패키지 서버를 {body.trust} 로 승인하려면 유효한 게시자 서명 필요 (서명 {v['signature']})",
                                  "verification": v})
    tools, proto = await fetch_tools(remote_url(row.server_json))
    result = scan(tools)
    result["verification"] = v
    if result["score"] >= BLOCK_SCORE:
        store().upsert(name, version, status="rejected", scan=result)
        raise HTTPException(409, f"승인 시점 재스캔 거부 (score {result['score']})")
    row = store().upsert(name, version, status="approved", trust=body.trust, pins={t["name"]: tool_hash(t) for t in tools}, protocol=proto, scan=result)
    return entry(row) | {"pins": row.pins}


@router.post("/admin/servers/{name:path}/versions/{version}/revoke")
async def revoke(name: str, version: str, authorization: str | None = Header(None)):
    require_admin(authorization)
    if store().get(name, version) is None:
        raise HTTPException(404, "server version not found")
    return entry(store().upsert(name, version, status="revoked"))


@router.put("/admin/allowlist/{tenant}")
async def put_allowlist(tenant: str, body: Allowlist, authorization: str | None = Header(None)):
    require_admin(authorization)
    store().set_allowlist(tenant, body.model_dump())
    return {"tenant": tenant, **body.model_dump()}


# ---- 커넥터 헬스·SLA (FR-MCP-SYS-06) ----
OPEN_AFTER = int(os.environ.get("ATLAS_MCP_HEALTH_FAILURES", "3"))


async def probe(url: str) -> tuple[bool, int, str | None]:
    """tools/list 성공 + (있으면) health 도구 성공 = 정상. 지연 ms."""
    import time as _t
    t0 = _t.monotonic()
    try:
        async with client_for(url, timeout=10) as c:
            tools = {t.name for t in await c.list_tools()}
            if "health" in tools:
                r = await c.call_tool("health", {}, raise_on_error=False)
                if r.is_error:
                    return False, int((_t.monotonic() - t0) * 1000), str(r.content[0].text if r.content else "health 실패")[:300]
        return True, int((_t.monotonic() - t0) * 1000), None
    except Exception as exc:  # noqa: BLE001
        return False, int((_t.monotonic() - t0) * 1000), f"{type(exc).__name__}: {exc}"[:300]


async def check_all() -> list[dict[str, Any]]:
    from atlas_agent_core import events
    out = []
    for row in store().list():
        if row.status != "approved":
            continue
        ok, ms, err = await probe(remote_url(row.server_json))
        prev, h = store().health(row.name, row.version), None
        h = store().record_health(row.name, row.version, ok, ms, err, OPEN_AFTER)
        if (prev or {}).get("state", "closed") != h["state"]:
            await events.publish(agent, "plt.mcp.sys.health", {"server": row.name, "version": row.version, "from": (prev or {}).get("state", "closed"),
                                                                "to": h["state"], "error": err, "latency_ms": ms})
        out.append({"name": row.name, "version": row.version, **h})
    return out


async def health_loop() -> None:
    import asyncio
    every = float(os.environ.get("ATLAS_MCP_HEALTH_INTERVAL_S", "30"))
    while True:
        await asyncio.sleep(every)
        try:
            await check_all()
        except Exception:  # noqa: BLE001,S110 — 다음 주기에 다시
            pass


@router.post("/admin/health/check")
async def health_now(authorization: str | None = Header(None)):
    require_admin(authorization)
    return {"items": await check_all()}


@router.get("/v0.1/servers/{name:path}/versions/{version}/health")
async def get_health(name: str, version: str):
    h = store().health(name, version)
    if h is None:
        raise HTTPException(404, "헬스 기록 없음")
    return {"name": name, "version": version, **h}


def allow_match(a: dict[str, Any], row: ServerRow) -> bool:
    """허용목록 항목 일치 — 이름 + (channel 포인터 또는 version glob, REG-07)."""
    if a["name"] != row.name:
        return False
    if a.get("channel"):
        ch = store().channel(row.name, a["channel"])
        return ch is not None and ch.version == row.version
    return fnmatch.fnmatch(row.version, a.get("version", "*"))


def effective(tenant: str) -> dict[str, Any]:
    """테넌트가 실제로 쓸 수 있는 서버 = 승인됨 ∩ 허용목록. 허용목록이 없으면 아무것도 허용하지 않는다(Shadow MCP 차단, FR-MCP-REG-03)."""
    policy = store().allowlist(tenant)
    if policy is None:
        return {"tenant": tenant, "servers": [], "tools": {"include": [], "exclude": []}, "note": "allowlist 없음 — 기본 거부"}
    servers, seen = [], set()
    for row in sorted(store().list(), key=lambda r: (r.name, [int(x) if x.isdigit() else x for x in re.split(r"[.-]", r.version)]), reverse=True):
        if row.status != "approved" or row.name in seen:
            continue
        if (store().health(row.name, row.version) or {}).get("state") == "open":   # 서킷 open — 호스트에 노출하지 않음 (SYS-06)
            continue
        if any(allow_match(a, row) for a in policy["servers"]):
            seen.add(row.name)                                # 이름당 1개(가장 높은 일치 버전) — 도구 이름 충돌 방지
            servers.append({"name": row.name, "version": row.version, "url": remote_url(row.server_json), "trust": row.trust,
                            "pins": row.pins or {}, "protocol": row.protocol, "meta": registry_meta(row.server_json)})
    servers.sort(key=lambda x: x["name"])
    return {"tenant": tenant, "servers": servers, "tools": policy["tools"]}


class ChannelIn(BaseModel):
    version: str
    force: bool = False          # 단계 건너뛰기(긴급 핫픽스) — 감사 이벤트에 남는다


@router.put("/admin/servers/{name:path}/channels/{channel}")
async def set_channel(name: str, channel: str, body: ChannelIn, authorization: str | None = Header(None)):
    """버전 고정·승격 (FR-MCP-REG-07): dev → staging → prod. 승인된 버전만 가리킬 수 있다."""
    require_admin(authorization)
    if channel not in ("dev", "staging", "prod"):
        raise HTTPException(422, "channel = dev | staging | prod")
    row = store().get(name, body.version)
    if row is None or row.status != "approved":
        raise HTTPException(409, f"{name}@{body.version} 이 승인 상태가 아님")
    prev = {"dev": None, "staging": "dev", "prod": "staging"}[channel]
    src = store().channel(name, prev) if prev else None
    if prev and not body.force and (src is None or src.version != body.version):
        raise HTTPException(409, f"{channel} 승격은 {prev} 채널이 같은 버전을 가리켜야 함")
    ch = store().set_channel(name, channel, body.version, "admin")
    await events.publish(agent, "plt.mcp.reg.channel_changed", {"server": name, "channel": channel, "version": body.version, "force": body.force})
    return {"name": name, "channel": channel, "version": ch.version, "history": ch.history}


@router.post("/admin/servers/{name:path}/channels/{channel}/rollback")
async def rollback_channel(name: str, channel: str, authorization: str | None = Header(None)):
    require_admin(authorization)
    ch = store().channel(name, channel)
    prev = next((h["from"] for h in reversed(ch.history) if h.get("from")), None) if ch else None
    if prev is None:
        raise HTTPException(409, "되돌릴 이전 버전 없음")
    ch = store().set_channel(name, channel, prev, "admin:rollback")
    await events.publish(agent, "plt.mcp.reg.channel_changed", {"server": name, "channel": channel, "version": prev, "rollback": True})
    return {"name": name, "channel": channel, "version": ch.version, "history": ch.history}


@router.get("/v0.1/servers/{name:path}/channels")
async def get_channels(name: str):
    return {"name": name, "channels": {c.channel: {"version": c.version, "history": c.history} for c in store().channels(name)}}


class KeyIn(BaseModel):
    public_key: str
    owner: str


@router.put("/admin/keys/{kid}")
async def put_key(kid: str, body: KeyIn, authorization: str | None = Header(None)):
    require_admin(authorization)
    store().put_key(kid, body.public_key, body.owner)
    return {"kid": kid, "owner": body.owner}


@router.get("/v0.1/tenants/{tenant}/effective")
async def get_effective(tenant: str):
    return effective(tenant)


async def search(ctx: TaskContext) -> dict[str, Any]:
    q = ctx.data.get("query") or ctx.text
    return {"servers": [entry(r) for r in store().list(q or None) if r.status == "approved"]}


agent = AgentSpec.from_file(MANIFEST, description="MCP-REG — 승인된 MCP 서버만 모으는 사설 레지스트리(허용목록·도구 고정·설치 전 스캔).",
                            skills=[Skill(id="mcp.registry", name="Registry search", handler=search,
                                          description="승인된 MCP 서버 검색 (Registry API v0.1 호환 데이터)")],
                            routers=[router], background=[health_loop])
