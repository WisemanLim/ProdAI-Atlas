"""MCP-HOST 게이트웨이 모드 (FR-MCP-HOST-04·05·07·08·10 · MCP-SEL-04 · MCP-TOOL-06).

`/t/{tenant}/mcp/{virtualServer}` 하나의 Streamable HTTP MCP 엔드포인트로 테넌트의 승인·허용 서버 도구를 모은다.
- 도구 이름 `{서버 별칭}__{도구}` · tools/list 결정적 정렬 · 가상 서버 = include/exclude glob + toolset 프로필(readOnly) — `default` = 전부.
- 정책은 MCP-HOST 라이브러리 그대로: 고정값(rug pull)·허용목록·승인 주석·가드레일 훅(인자·결과 검사, 오염 후 외부 쓰기 차단)·결과 크기 제한·OAuth 업스트림.
- 양방향 중계: 업스트림의 진행률·입력 요청(elicitation)을 호출한 클라이언트로 그대로 넘긴다(호출 전용 업스트림 세션).
- 인바운드 인증: Manager 가 발급한 aud=atlas-mcp-gateway JWT(ES256, Manager JWKS) — 조직 클레임 = 경로 테넌트. 토큰은 업스트림으로 절대 넘기지 않는다.
- Origin 허용목록 (DNS rebinding 방지) — Origin 헤더가 있고 목록 밖이면 403.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, Header, HTTPException
from fastmcp import FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.base import InputRequiredToolResult
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core import AgentSpec, Skill, TaskContext, secrets
from atlas_agent_core.tasks import Base
from units.mcp_host import McpHost, toolsets

MANIFEST = Path(__file__).with_name("atlas-agent.json")
log = logging.getLogger("atlas.mcp_gw")
router = APIRouter()
_HOSTS: dict[str, McpHost] = {}
_SPECS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_SESS: dict[str, dict[str, Any]] = {}       # (테넌트, 실행 id) 별 실행 문맥 — 오염 표시 유지 (HOST-08)
_JWKS: dict[str, Any] = {}
PATH_RE = re.compile(r"^/(?P<tenant>[a-zA-Z0-9._-]{1,64})/mcp/(?P<vs>[a-zA-Z0-9._-]{1,64})/?$")


class VirtualServerRow(Base):
    __tablename__ = "mcp_virtual_servers"
    tenant: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    include: Mapped[list] = mapped_column(JSON)
    exclude: Mapped[list] = mapped_column(JSON)
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def cfg() -> dict[str, Any]:
    return agent.manifest["spec"]["config"]


def vserver(tenant: str, name: str) -> dict[str, Any] | None:
    if name == "default":
        return {"include": [], "exclude": [], "profile": None}
    with Session(agent.resources["engine"]) as s:
        r = s.get(VirtualServerRow, (tenant, name))
    return {"include": r.include, "exclude": r.exclude, "profile": r.profile} if r else None


def host(tenant: str) -> McpHost:
    reg = os.environ.get("ATLAS_MCP_REGISTRY_URL", "http://agent-mcp-reg:8000")
    return _HOSTS.setdefault(tenant, McpHost(reg, tenant))


async def specs_for(tenant: str, vs: str) -> dict[str, Any]:
    key = (tenant, vs)
    hit = _SPECS.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    v = vserver(tenant, vs)
    if v is None:
        raise ToolError(f"가상 서버 {vs} 없음")
    specs, _ = await host(tenant).tools(profile=v["profile"], include=v["include"], exclude=v["exclude"])
    out = {s.name: s for s in specs}
    _SPECS[key] = (time.monotonic() + float(cfg()["specTtlS"]), out)
    return out


def scope() -> tuple[str, str, str]:
    h = get_http_headers(include_all=True)
    return h.get("x-atlas-gw-tenant", ""), h.get("x-atlas-gw-vs", "default"), h.get("x-atlas-gw-sub", "anonymous")


class GwTool(Tool):
    """게이트웨이가 노출하는 도구 메타(실행은 미들웨어가 가로챈다)."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:  # pragma: no cover — on_call_tool 이 처리
        raise ToolError("gateway tool must be called through the gateway middleware")


def tool_meta(s: Any) -> GwTool:
    ro = s.side_effect == "read"
    return GwTool(name=s.name, description=s.description, parameters=s.input_schema or {"type": "object"},
                  annotations=ToolAnnotations(title=s.annotations.get("title") or s.name, readOnlyHint=ro, destructiveHint=s.side_effect == "irreversible"),
                  meta={"io.prodai.atlas/server": s.annotations.get("server"), "io.prodai.atlas/risk": s.risk})


class GatewayMiddleware(Middleware):
    async def on_list_tools(self, context, call_next):
        tenant, vs, _ = scope()
        specs = await specs_for(tenant, vs)
        return [tool_meta(specs[n]) for n in sorted(specs)]

    async def on_call_tool(self, context, call_next):
        tenant, vs, sub = scope()
        name, args = context.message.name, dict(context.message.arguments or {})
        spec = (await specs_for(tenant, vs)).get(name)
        if spec is None:
            raise ToolError(f"이 가상 서버에 없는 도구 {name}")
        if spec.annotations.get("approval") == "require_approval":
            raise ToolError(f"{name} 는 승인 대상 — 승인함(X-HITL)을 거치는 에이전트 루프에서 호출")
        fctx = context.fastmcp_context
        # 2026 프로토콜은 무상태(요청마다 세션 id 가 바뀐다) — 오염 표시는 호출자 실행 id(x-atlas-run-id) 단위, 없으면 주체 단위(보수적)
        run_id = get_http_headers(include_all=True).get("x-atlas-run-id") or f"sub:{sub}"
        skey = f"{tenant}:{run_id}"
        for k in [k for k, v in _SESS.items() if v.get("_at", 0) < time.monotonic() - float(cfg()["taintTtlS"])]:
            _SESS.pop(k, None)
        run_ctx = _SESS.setdefault(skey, {"tenant": tenant, "sub": sub})
        run_ctx["_at"] = time.monotonic()

        progress_q: asyncio.Queue = asyncio.Queue()

        async def progress(p: float, total: float | None, message: str | None) -> None:
            # 업스트림 수신 태스크에서 불린다 — 하위 요청 문맥이 없으므로 큐에 넣고 이 요청 태스크가 전달한다
            progress_q.put_nowait((p, total, message))

        async def elicit(message: str, _rt: Any, params: Any, _c: Any) -> ElicitResult:
            """2025 서버의 역요청(elicitation/create) — 하위가 2025 세션이면 그대로 중계, 아니면 거절(2026 은 MRTR 로 정규화)."""
            if fctx is None:
                return ElicitResult(action="decline")
            try:
                r = await fctx.session.elicit_form(message, params.requested_schema, related_request_id=fctx.request_id)
            except Exception:  # noqa: BLE001 — 2026 하위 세션은 서버 발 요청 불가
                return ElicitResult(action="decline")
            return ElicitResult(action=r.action, content=r.content)

        run_ctx["mcp_relay"] = {"progress_handler": progress, "elicitation_handler": elicit,
                                # 2026 MRTR: 하위 클라이언트가 답한 입력·상태를 업스트림에 그대로 재발행
                                "input_responses": getattr(fctx, "input_responses", None), "request_state": getattr(fctx, "request_state", None)}
        try:
            task = asyncio.ensure_future(spec.handler(args, run_ctx))
            while not task.done():
                getter = asyncio.ensure_future(progress_q.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    if fctx is not None:
                        await fctx.report_progress(*getter.result())
                else:
                    getter.cancel()
            while not progress_q.empty() and fctx is not None:
                await fctx.report_progress(*progress_q.get_nowait())
            out = task.result()
        except PermissionError as exc:
            raise ToolError(str(exc)) from exc
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            run_ctx.pop("mcp_relay", None)
        if isinstance(out, dict) and "__input_required__" in out:      # 업스트림 입력 요청 → 하위 클라이언트로 (HOST-05)
            return InputRequiredToolResult(out["__input_required__"])
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        return ToolResult(content=text, structured_content=out if isinstance(out, dict) else {"result": out})


gw = FastMCP("atlas-mcp-gateway", instructions="ProdAI Atlas MCP 게이트웨이 — 테넌트 승인 서버의 도구를 하나의 엔드포인트로")
gw.add_middleware(GatewayMiddleware())
_inner = gw.http_app(path="/mcp")


async def verify_bearer(token: str) -> dict[str, Any]:
    mgr = (os.environ.get("ATLAS_MANAGER_URL") or "http://manager:8080").rstrip("/")
    kid = jwt.get_unverified_header(token).get("kid")
    keys = _JWKS.get("keys")
    if not keys or not any(k.get("kid") == kid for k in keys):
        async with httpx.AsyncClient(timeout=5) as c:
            _JWKS.update((await c.get(f"{mgr}/.well-known/jwks.json")).json())
        keys = _JWKS.get("keys", [])
    k = next((k for k in keys if k.get("kid") == kid), None)
    if k is None:
        raise jwt.InvalidTokenError("알 수 없는 서명 키")
    return jwt.decode(token, jwt.PyJWK(k).key, algorithms=["ES256"], audience="atlas-mcp-gateway")


class GatewayASGI:
    """/t/{tenant}/mcp/{vs} → FastMCP /mcp. Origin·인증 검사 후 테넌트·가상 서버·주체를 내부 헤더로 싣는다(인바운드 토큰은 지운다)."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.router = self                      # SDK 가 router.lifespan_context(sub) 로 하위 앱 lifespan 을 연다

    def lifespan_context(self, _app: Any):
        return self.inner.router.lifespan_context(self.inner)

    async def __call__(self, scope_: dict, receive, send) -> None:
        if scope_["type"] != "http":
            return await self.inner(scope_, receive, send)
        path, root = scope_.get("path", ""), scope_.get("root_path", "")
        if root and path.startswith(root):             # Starlette Mount 는 전체 경로 + root_path 를 넘긴다
            path = path[len(root):]
        m = PATH_RE.match(path)
        if not m:
            return await deny(send, 404, "경로 = /t/{tenant}/mcp/{virtualServer}")
        headers = {k.decode().lower(): v.decode() for k, v in scope_.get("headers", [])}
        origin = headers.get("origin")
        if origin and origin not in cfg()["allowedOrigins"]:
            return await deny(send, 403, f"허용되지 않은 Origin {origin}")
        sub, tenant = "anonymous", m.group("tenant")
        auth = headers.get("authorization", "")
        mode = os.environ.get("ATLAS_MCP_GW_AUTH", "optional")
        if auth.lower().startswith("bearer "):
            try:
                claims = await verify_bearer(auth[7:])
            except (jwt.PyJWTError, httpx.HTTPError, ValueError) as exc:
                return await deny(send, 401, f"토큰 거부: {exc}")
            if claims.get("atlas_org") != tenant:
                return await deny(send, 403, f"토큰 조직 {claims.get('atlas_org')} ≠ 경로 테넌트 {tenant}")
            sub = claims.get("sub", "unknown")
        elif mode == "required":
            return await deny(send, 401, "Bearer 토큰 필요 (Manager POST /api/v1/agents/{id}/token?audience=atlas-mcp-gateway)")
        keep = [(k, v) for k, v in scope_.get("headers", []) if k.decode().lower() not in ("authorization", "cookie") and not k.decode().lower().startswith("x-atlas-gw-")]
        keep += [(b"x-atlas-gw-tenant", tenant.encode()), (b"x-atlas-gw-vs", m.group("vs").encode()), (b"x-atlas-gw-sub", sub.encode())]
        inner_scope = {**scope_, "path": "/mcp", "raw_path": b"/mcp", "headers": keep, "root_path": ""}
        await self.inner(inner_scope, receive, send)


async def deny(send, code: int, msg: str) -> None:
    body = json.dumps({"error": msg}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": code, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


# ---- 관리: 가상 서버 · toolset ----

async def require_admin(authorization: str | None) -> None:
    tok = await secrets.get("mcp-reg-admin-token", env="ATLAS_MCP_REG_ADMIN_TOKEN", dev_default="")
    if not tok or "CHANGE_ME" in tok or authorization != f"Bearer {tok}":
        raise HTTPException(401, "관리 토큰 필요")


class VirtualServerIn(BaseModel):
    include: list[str] = []
    exclude: list[str] = []
    profile: str | None = None


@router.put("/admin/virtual-servers/{tenant}/{name}")
async def put_vs(tenant: str, name: str, body: VirtualServerIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    if body.profile and body.profile not in toolsets():
        raise HTTPException(422, f"toolset 프로필 = {sorted(toolsets())}")
    if name == "default":
        raise HTTPException(422, "default 는 예약 이름(전체)")
    with Session(agent.resources["engine"]) as s, s.begin():
        s.merge(VirtualServerRow(tenant=tenant, name=name, include=body.include, exclude=body.exclude, profile=body.profile, updated_at=datetime.now(UTC)))
    for k in [k for k in _SPECS if k[0] == tenant]:
        _SPECS.pop(k, None)
    return {"tenant": tenant, "name": name, **body.model_dump(), "url": f"/t/{tenant}/mcp/{name}"}


@router.get("/v1/virtual-servers/{tenant}")
async def list_vs(tenant: str):
    with Session(agent.resources["engine"]) as s:
        rows = [r for r in s.query(VirtualServerRow).filter(VirtualServerRow.tenant == tenant)]
    return {"items": [{"name": "default", "include": [], "exclude": [], "profile": None, "url": f"/t/{tenant}/mcp/default"}] +
            [{"name": r.name, "include": r.include, "exclude": r.exclude, "profile": r.profile, "url": f"/t/{tenant}/mcp/{r.name}"} for r in rows]}


@router.get("/v1/toolsets")
async def get_toolsets():
    return {"profiles": toolsets()}


async def gateway_skill(ctx: TaskContext) -> dict[str, Any]:
    vs = ctx.data.get("virtual_server", "default")
    specs = await specs_for(ctx.tenant, vs)
    return {"url": f"/t/{ctx.tenant}/mcp/{vs}", "tools": sorted(specs), "glob": [n for n in sorted(specs) if fnmatch.fnmatch(n, ctx.data.get("filter", "*"))]}


agent = AgentSpec.from_file(MANIFEST, description="MCP 게이트웨이 — 테넌트 승인 MCP 서버를 하나의 엔드포인트로 (네임스페이스·가상 서버·정책 훅·양방향 중계·OAuth 업스트림)",
                            skills=[Skill(id="mcp.gateway", name="MCP gateway", handler=gateway_skill, description="가상 서버 엔드포인트와 도구 목록")],
                            routers=[router], mounts=[("/t", GatewayASGI(_inner))])
