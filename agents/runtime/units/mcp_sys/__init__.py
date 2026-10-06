"""MCP-SYS Systems Connector + MCP-RES Resources — 표준 커넥터 팩(GitHub·Slack·Jira·ERP DB·웹)을 하나의 MCP 서버로 (Streamable HTTP `/connectors/mcp`).

- 자격증명 브로커 (SYS-02): 시스템별 토큰은 Manager 비밀 중개로만 받아 업스트림 호출에 주입 — 도구 결과·에이전트에는 절대 나가지 않는다.
- egress (SYS-04): 모든 업스트림 HTTP 는 egress 프록시(허용목록·사설/메타데이터 차단)를 거친다. 기동 시 자기 정책을 등록·갱신.
- DB 어댑터 (SYS-05): 이름 붙은 질의만(파라미터화)·읽기 전용 트랜잭션(PostgreSQL 은 SELECT 전용 역할)·행 수 제한. 쓰기는 별도 도구(승인 대상).
- 헬스 (SYS-06): `health` 도구 — 업스트림 가용성. MCP-REG 가 주기 호출해 서킷을 연다.
- 리소스 (RES-01~06): db://erp/{table} · file://docs/{path} · log://sbx/recent · URI 템플릿 완성 · 변경 알림(SSE) · ttlMs/cacheScope 메타 · 프로젝트 경계(-32002) · PII 마스킹·기밀 요약.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets as pysecrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from atlas_agent_core import AgentSpec, Skill, TaskContext, otel, secrets

MANIFEST = Path(__file__).with_name("atlas-agent.json")
DOCS = Path(__file__).with_name("docs")
OUT_OF_BOUNDARY = -32002
TRANSPORT: httpx.AsyncBaseTransport | None = None
STATE: dict[str, Any] = {"proxy": None}

mcp = FastMCP("atlas-connectors", instructions="ProdAI Atlas 표준 커넥터 팩 — GitHub·Slack·Jira·ERP DB·웹. 자격증명은 서버가 주입한다.")


def cfg() -> dict[str, Any]:
    return agent.manifest["spec"]["config"]


def engine():
    return agent.resources["engine"]


def project() -> str:
    try:
        return get_http_headers(include_all=True).get("x-atlas-project", "default")
    except Exception:  # noqa: BLE001 — HTTP 문맥 밖(시험·내부 호출)
        return "default"


def boundary() -> dict[str, Any]:
    b = cfg()["boundaries"]
    return b.get(project(), b["default"])


def deny(msg: str) -> MCPError:
    return MCPError(OUT_OF_BOUNDARY, f"경계 밖 요청: {msg} (project {project()})")


def client(timeout: float = 15) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=TRANSPORT, proxy=None if TRANSPORT else STATE["proxy"])   # 시험 transport 는 프록시 우회


async def upstream(system: str, method: str, path: str, body: Any = None) -> Any:
    """업스트림 호출 — 토큰은 비밀 중개에서 꺼내 헤더로만. 401 이면 새 값을 받아 1회 재시도(회전 대비)."""
    base = cfg()["upstreams"][system]
    for fresh in (False, True):
        try:
            tok = await secrets.get(f"{system}-token", env=f"ATLAS_{system.upper()}_TOKEN", dev_default=f"fake-{system}-token", fresh=fresh)
        except secrets.SecretUnavailable as exc:
            raise ValueError(f"{system} 자격증명 미설정 — 조직 비밀 {system}-token") from exc
        async with client() as c:
            r = await c.request(method, base + path, json=body, headers={"Authorization": f"Bearer {tok}", **otel.inject({})})
        if r.status_code != 401:
            break
    if r.status_code >= 400:
        raise ValueError(f"{system} {r.status_code}: {r.text[:200]}")
    return r.json()


# ---- 도구: 표준 커넥터 팩 (SYS-01) ----

@mcp.tool(title="GitHub 이슈 목록", annotations={"readOnlyHint": True, "openWorldHint": True})
async def github_list_issues(repo: str = "atlas/app") -> list[dict]:
    """GitHub 저장소의 이슈를 조회한다."""
    return await upstream("github", "GET", f"/repos/{repo}/issues")


@mcp.tool(title="GitHub 이슈 생성", annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def github_create_issue(title: str, repo: str = "atlas/app") -> dict:
    """GitHub 저장소에 이슈를 만든다."""
    return await upstream("github", "POST", f"/repos/{repo}/issues", {"title": title})


@mcp.tool(title="Slack 메시지", annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def slack_post_message(channel: str, message: str) -> dict:
    """Slack 채널에 메시지를 보낸다."""
    return await upstream("slack", "POST", "/chat.postMessage", {"channel": channel, "text": message})


@mcp.tool(title="Jira 검색", annotations={"readOnlyHint": True, "openWorldHint": True})
async def jira_search(jql: str = "project = ATL") -> dict:
    """Jira 이슈를 JQL 로 검색한다."""
    return await upstream("jira", "GET", f"/rest/api/3/search?jql={jql}")


@mcp.tool(title="웹 가져오기", annotations={"readOnlyHint": True, "openWorldHint": True})
async def web_fetch(url: str) -> dict:
    """웹 페이지 본문을 가져온다 (egress 허용목록 안에서만)."""
    async with client() as c:
        try:
            r = await c.get(url)
        except httpx.ProxyError as exc:
            return {"url": url, "blocked": True, "error": str(exc)[:200]}
    if r.status_code == 403 and r.headers.get("x-egress-reason"):
        return {"url": url, "blocked": True, "reason": r.headers["x-egress-reason"]}
    return {"url": url, "status": r.status_code, "text": r.text[:4000]}


@mcp.tool(title="커넥터 헬스", annotations={"readOnlyHint": True})
async def health() -> dict:
    """연결된 업스트림 시스템 가용성 — 가벼운 읽기 호출 (MCP-REG 헬스·SLA 가 주기 호출, 실패면 오류)."""
    out = {}
    for name, path in (("github", "/repos/atlas/app/issues"), ("jira", "/rest/api/3/search?jql=limit")):
        try:
            await upstream(name, "GET", path)
            out[name] = "ok"
        except (ValueError, httpx.HTTPError) as exc:
            out[name] = f"down: {str(exc)[:80]}"
    if any(v != "ok" for v in out.values()):
        raise ValueError(f"업스트림 장애: {out}")
    return out


# ---- ERP DB 어댑터 (SYS-05) ----

QUERIES = {
    "orders_by_region": "SELECT id, region, amount, customer_id FROM erp_orders WHERE region = :region ORDER BY id",
    "order_total": "SELECT region, SUM(amount) AS total, COUNT(*) AS n FROM erp_orders GROUP BY region ORDER BY region",
    "customer_orders": "SELECT o.id, o.amount FROM erp_orders o WHERE o.customer_id = :customer_id ORDER BY o.id",
}
TABLES = {"erp_orders": ["id", "region", "amount", "customer_id"], "erp_customers": ["id", "name", "email", "phone"]}


def erp_setup() -> None:
    """데모 ERP 테이블·읽기 전용 역할 (PostgreSQL: SELECT 만 가진 atlas_erp_ro)."""
    with engine().begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS erp_customers (id INTEGER PRIMARY KEY, name VARCHAR(64), email VARCHAR(128), phone VARCHAR(32))"))
        c.execute(text("CREATE TABLE IF NOT EXISTS erp_orders (id INTEGER PRIMARY KEY, region VARCHAR(8), amount INTEGER, customer_id INTEGER)"))
        if not c.execute(text("SELECT COUNT(*) FROM erp_customers")).scalar():
            c.execute(text("INSERT INTO erp_customers VALUES (1, '김하나', 'hana@example.com', '010-1234-5678'), (2, '이두리', 'duri@example.com', '010-2222-3333')"))
            c.execute(text("INSERT INTO erp_orders VALUES (1, 'KR', 12000, 1), (2, 'US', 8000, 2), (3, 'KR', 30000, 2)"))
        if engine().dialect.name == "postgresql":
            if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'atlas_erp_ro'")).scalar():
                c.execute(text("CREATE ROLE atlas_erp_ro NOLOGIN"))
            c.execute(text("GRANT SELECT ON erp_orders, erp_customers TO atlas_erp_ro"))


_READY = {"erp": False}


def ro_query(sql: str, params: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    if not _READY["erp"]:
        erp_setup()
        _READY["erp"] = True
    limit = limit or int(cfg()["rowLimit"])
    with engine().connect() as c:
        if engine().dialect.name == "postgresql":
            c.execute(text("SET TRANSACTION READ ONLY"))
            c.execute(text("SET LOCAL ROLE atlas_erp_ro"))
        else:
            c.exec_driver_sql("PRAGMA query_only = ON")
        try:
            rows = [dict(r._mapping) for r in c.execute(text(sql), params).fetchmany(limit + 1)]
        finally:
            if engine().dialect.name != "postgresql":
                c.exec_driver_sql("PRAGMA query_only = OFF")
            c.rollback()
    return rows


def row_filter(table: str) -> str:
    f = boundary().get("row_filter", {}).get(table)
    return f" WHERE {f}" if f else ""


@mcp.tool(title="ERP 조회", annotations={"readOnlyHint": True, "openWorldHint": False})
async def db_query(name: str, params: dict | None = None) -> dict:
    """이름 붙은 ERP 질의(화이트리스트·파라미터화·읽기 전용·행 제한)를 실행한다. 이름: orders_by_region · order_total · customer_orders."""
    if name not in QUERIES:
        raise ValueError(f"허용되지 않은 질의 {name} — 사용 가능: {sorted(QUERIES)}")
    if "erp_orders" not in boundary()["db_tables"]:
        raise deny("erp_orders")
    rows = ro_query(QUERIES[name], params or {})
    lim = int(cfg()["rowLimit"])
    return {"query": name, "rows": rows[:lim], "truncated": len(rows) > lim}


@mcp.tool(title="ERP 주문 생성", annotations={"readOnlyHint": False, "destructiveHint": False})
async def erp_create_order(region: str, amount: int, customer_id: int) -> dict:
    """ERP 에 주문을 만든다 (쓰기 — 승인 대상 도구)."""
    ro_query("SELECT 1", {})          # 테이블 준비
    with engine().begin() as c:
        nid = (c.execute(text("SELECT COALESCE(MAX(id), 0) + 1 FROM erp_orders")).scalar())
        c.execute(text("INSERT INTO erp_orders VALUES (:i, :r, :a, :c)"), {"i": nid, "r": region, "a": amount, "c": customer_id})
    return {"id": nid}


# ---- 리소스 (RES-01·02·04·05·06) ----



async def mask(texts: list[str]) -> list[str]:
    """PII 마스킹 (GR-IO retrieval 경계, RES-06) — 가드 장애 시 원문을 내보내지 않는다(fail-closed)."""
    try:
        async with httpx.AsyncClient(timeout=10, transport=TRANSPORT) as c:
            r = await c.post((os.environ.get("ATLAS_GUARD_URL") or "http://agent-gr:8000") + "/v1/guard/check",
                             json={"tenant": "default", "boundary": "retrieval", "contents": texts})
        r.raise_for_status()
        return r.json().get("contents") or texts
    except (httpx.HTTPError, ValueError):
        return ["[masked: guard unavailable]" for _ in texts]


@mcp.resource("db://erp/{table}", name="erp-table", description="ERP 테이블 스키마와 표본 행 (경계·데이터 등급 적용)", mime_type="application/json",
              meta={"ttlMs": 30000, "cacheScope": "tenant"})
async def erp_table(table: str) -> str:
    if table not in TABLES:
        raise ValueError(f"알 수 없는 테이블 {table}")
    if table not in boundary()["db_tables"]:
        raise deny(f"db://erp/{table}")
    rows = ro_query(f"SELECT * FROM {table}{row_filter(table)} ORDER BY id", {}, limit=5)
    cls = cfg()["dataClass"].get(table, "internal")
    if cls in ("confidential", "restricted"):   # 기밀 등급은 원문 대신 요약·해시
        body = {"table": table, "dataClass": cls, "columns": TABLES[table], "rows": len(rows),
                "sample": [{"id": r["id"], "sha256": hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:12]} for r in rows]}
        return json.dumps(body, ensure_ascii=False)
    raw = json.dumps({"table": table, "dataClass": cls, "columns": TABLES[table], "rows": rows}, ensure_ascii=False, default=str)
    return (await mask([raw]))[0]


@mcp.resource("file://docs/{path*}", name="docs", description="운영 문서 (프로젝트 경로 경계)", mime_type="text/markdown",
              meta={"ttlMs": 30000, "cacheScope": "tenant"})
async def doc_file(path: str) -> str:
    rel = path.lstrip("/")
    if ".." in rel.split("/") or not any(rel.startswith(p) for p in boundary()["file_prefixes"]):
        raise deny(f"file://docs/{rel}")
    p = (DOCS / rel).resolve()
    if DOCS.resolve() not in p.parents or not p.is_file():
        raise ValueError(f"문서 없음 {rel}")
    return (await mask([p.read_text(encoding="utf-8")]))[0]


@mcp.resource("log://sbx/recent", name="sandbox-runs", description="최근 샌드박스 실행 기록(코드 해시·상태·송신 결정)", mime_type="application/json",
              meta={"ttlMs": 5000, "cacheScope": "tenant"})
async def sbx_log() -> str:
    try:
        async with httpx.AsyncClient(timeout=10, transport=TRANSPORT) as c:
            r = await c.get((os.environ.get("ATLAS_SBX_URL") or "http://agent-sbx:8000") + "/v1/runs", params={"limit": 20})
        return json.dumps(r.json(), ensure_ascii=False)
    except (httpx.HTTPError, ValueError) as exc:
        return json.dumps({"error": str(exc)[:200]})


@mcp.resource("file://docs/index", name="docs-index", description="경계 안 문서 목록", mime_type="application/json", meta={"ttlMs": 30000, "cacheScope": "tenant"})
async def doc_index() -> str:
    items = [str(p.relative_to(DOCS)) for p in sorted(DOCS.rglob("*.md")) if any(str(p.relative_to(DOCS)).startswith(x) for x in boundary()["file_prefixes"])]
    return json.dumps({"files": items}, ensure_ascii=False)


@mcp.completion
async def complete(ref, argument, context):
    """URI 템플릿 인자 자동 완성 (RES-02): db://erp/{table} → 경계 안 테이블, file://docs/{path} → 문서 경로."""
    uri = getattr(ref, "uri", "")
    if uri.startswith("db://erp/") and argument.name == "table":
        return [t for t in boundary()["db_tables"] if t.startswith(argument.value or "")]
    if uri.startswith("file://docs/") and argument.name == "path":
        return [str(p.relative_to(DOCS)) for p in sorted(DOCS.rglob("*.md"))
                if any(str(p.relative_to(DOCS)).startswith(x) for x in boundary()["file_prefixes"]) and str(p.relative_to(DOCS)).startswith(argument.value or "")]
    return []


# ---- 변경 알림 (RES-03) — 리소스 지문을 감시해 notifications/resources/updated 를 SSE 로 ----

def fingerprint(uri: str) -> str:
    m = re.match(r"^db://erp/(\w+)$", uri)
    if m and m.group(1) in TABLES:
        with engine().connect() as c:
            n, mx = c.execute(text(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {m.group(1)}")).one()
        return f"{n}:{mx}"
    m = re.match(r"^file://docs/(.+)$", uri)
    if m:
        p = (DOCS / m.group(1)).resolve()
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and DOCS.resolve() in p.parents else "missing"
    raise ValueError(f"구독할 수 없는 URI {uri}")


router = APIRouter()


@router.get("/resources/subscribe")
async def subscribe(uri: str, request: Request, interval_ms: int = 500, max_s: int = 60):
    """2025 계열 resources/subscribe 대응 — 서버측 변경 감지(DB 행 지문·파일 해시) 후 JSON-RPC 알림을 SSE 로 흘린다."""
    first = fingerprint(uri)

    async def gen():
        last = first
        yield f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/resources/subscribed', 'params': {'uri': uri}})}\n\n"
        for _ in range(int(max_s * 1000 / max(100, interval_ms))):
            if await request.is_disconnected():
                return
            await asyncio.sleep(max(100, interval_ms) / 1000)
            cur = fingerprint(uri)
            if cur != last:
                last = cur
                yield f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/resources/updated', 'params': {'uri': uri}})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---- egress 정책 등록·갱신 (SYS-04) ----

async def egress_loop() -> None:
    proxy_admin = (os.environ.get("ATLAS_EGRESS_ADMIN_URL") or "http://egress-proxy:8080").rstrip("/")
    proxy = (os.environ.get("ATLAS_EGRESS_PROXY_URL") or "http://egress-proxy:3128").rstrip("/")
    pid = "mcp-sys-" + pysecrets.token_hex(4)
    erp_setup()
    _READY["erp"] = True
    while True:
        try:
            tok = await secrets.get("egress-admin-token", env="ATLAS_EGRESS_ADMIN_TOKEN", dev_default="local-egress-admin")
            ptok = pysecrets.token_urlsafe(16)
            async with httpx.AsyncClient(timeout=10, transport=TRANSPORT) as c:
                r = await c.put(f"{proxy_admin}/policies/{pid}", headers={"X-Egress-Admin": tok}, json={"token": ptok, "allow": cfg()["egressAllow"], "ttl_s": 3600})
                r.raise_for_status()
            STATE["proxy"] = proxy.replace("://", f"://{pid}:{ptok}@", 1)
            await asyncio.sleep(1800)
        except (httpx.HTTPError, secrets.SecretUnavailable):
            await asyncio.sleep(3)


async def systems_skill(ctx: TaskContext) -> dict[str, Any]:
    return {"mcp": "/connectors/mcp", "tools": [t.name for t in await mcp.list_tools()], "resources": [str(r.uri) for r in await mcp.list_resources()],
            "templates": [t.uri_template for t in await mcp.list_resource_templates()]}


agent = AgentSpec.from_file(MANIFEST, description="MCP-SYS 표준 커넥터 팩 + MCP-RES 리소스 — GitHub·Slack·Jira·ERP DB·웹, 자격증명 주입·egress 통제·읽기 전용 DB·헬스",
                            skills=[Skill(id="mcp.systems", name="Systems connectors", handler=systems_skill, description="커넥터 MCP 서버 위치·도구·리소스 목록")],
                            routers=[router], background=[egress_loop], mounts=[("/connectors", mcp.http_app(path="/mcp"))])
