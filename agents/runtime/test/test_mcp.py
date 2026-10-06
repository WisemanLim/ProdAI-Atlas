"""MCP 핵심 — 레지스트리(스캔·고정·허용목록) · 호스트(풀·고정 검사·승인 규칙·패스스루 금지) · 선별(BM25) · 루프 연동 · OpenAPI→MCP."""
import asyncio
import importlib
import json
import os
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from loop_fakes import ScriptGateway

import units.mcp_reg as reg
from atlas_agent_core import create_app
from units import al_exit, mcp_sel
from units.al_act import BUILTINS, ActConfig
from units.al_run.clients import PlannerClient
from units.al_run.engine import LoopEngine
from units.al_run.store import LoopStore
from units.mcp_host import McpHost, upstream_headers

ADMIN = {"authorization": "Bearer test-reg-admin"}


def demo_server(poison: bool):
    os.environ["DEMO_POISON"] = "true" if poison else "false"
    from mcp_servers import demo
    return importlib.reload(demo).server


CLEAN, POISONED = demo_server(False), demo_server(True)
SERVERS = {"http://demo/mcp": CLEAN, "http://evil/mcp": POISONED}


async def fake_fetch(url):
    async with Client(SERVERS[url]) as c:
        tools = await c.list_tools()
        return [{"name": t.name, "title": t.title, "description": t.description, "input_schema": t.input_schema} for t in tools], c.protocol_version


@pytest.fixture(scope="module")
def reg_client(client):
    from app import db
    os.environ["ATLAS_MCP_REG_ADMIN_TOKEN"] = "test-reg-admin"
    with TestClient(create_app(reg.agent, engine=db.engine, public_url="http://mcp-reg:8000")) as c:
        yield c


@pytest.fixture(autouse=True)
def patch_fetch(monkeypatch):
    monkeypatch.setattr(reg, "fetch_tools", fake_fetch)


def server_json(name, url):
    return {"name": name, "version": "1.0.0", "description": "demo", "remotes": [{"type": "streamable-http", "url": url}]}


def test_scan_scores_clean_and_poisoned():
    clean, _ = asyncio.run(fake_fetch("http://demo/mcp"))
    bad, _ = asyncio.run(fake_fetch("http://evil/mcp"))
    assert reg.scan(clean)["score"] == 0
    result = reg.scan(bad)
    rules = {f["rule"] for f in result["findings"]["search_docs"]}
    assert result["score"] >= 70 and {"instruction_tag", "override_phrase", "secret_path"} <= rules


def test_registry_lifecycle_and_allowlist(reg_client):
    assert reg_client.post("/admin/servers", json=server_json("io.prodai/demo", "http://demo/mcp")).status_code == 401
    r = reg_client.post("/admin/servers", json=server_json("io.prodai/demo", "http://demo/mcp"), headers=ADMIN).json()
    assert r["_meta"][reg.META]["status"] == "scanned" and r["_meta"][reg.META]["protocol"] == "2026-07-28"
    a = reg_client.post("/admin/servers/io.prodai/demo/versions/1.0.0/approve", json={"trust": "verified"}, headers=ADMIN).json()
    assert a["_meta"][reg.META]["status"] == "approved" and set(a["pins"]) == {"search_docs", "get_weather", "create_ticket", "delete_records"}
    listed = reg_client.get("/v0.1/servers", params={"search": "demo"}).json()
    assert listed["metadata"]["count"] == 1 and listed["servers"][0]["server"]["name"] == "io.prodai/demo"
    assert reg_client.get("/v0.1/servers/io.prodai/demo/versions/1.0.0").json()["pins"]["search_docs"] == a["pins"]["search_docs"]
    assert reg_client.get("/v0.1/tenants/acme/effective").json()["servers"] == []          # 허용목록 없음 → 기본 거부
    reg_client.put("/admin/allowlist/acme", json={"servers": [{"name": "io.prodai/demo", "version": "1.*"}],
                                                  "tools": {"include": ["demo__*"], "exclude": ["demo__get_weather"]}}, headers=ADMIN)
    eff = reg_client.get("/v0.1/tenants/acme/effective").json()
    assert [s["name"] for s in eff["servers"]] == ["io.prodai/demo"] and eff["tools"]["exclude"] == ["demo__get_weather"]


def test_poisoned_server_rejected(reg_client):
    r = reg_client.post("/admin/servers", json=server_json("io.evil/tools", "http://evil/mcp"), headers=ADMIN).json()
    assert r["_meta"][reg.META]["status"] == "rejected" and r["scan"]["score"] >= 70
    assert reg_client.post("/admin/servers/io.evil/tools/versions/1.0.0/approve", json={"trust": "verified"}, headers=ADMIN).status_code == 409


def host_for(effective):
    h = McpHost("http://reg", "acme")

    async def eff():
        return effective
    h.effective = eff
    return h


def pins_of(server):
    tools, _ = asyncio.run(fake_fetch("http://demo/mcp" if server is CLEAN else "http://evil/mcp"))
    return {t["name"]: reg.tool_hash(t) for t in tools}


def effective(server, trust="verified", include=("*",), exclude=(), pins=None):
    return {"servers": [{"name": "io.prodai/demo", "url": server, "trust": trust, "pins": pins or pins_of(CLEAN)}],
            "tools": {"include": list(include), "exclude": list(exclude)}}


def test_host_maps_tools_with_trusted_annotations(monkeypatch):
    monkeypatch.setenv("ATLAS_MCP_APPROVAL_RULES", '[{"match":"demo__create_*","action":"require_approval"}]')
    specs, report = asyncio.run(host_for(effective(CLEAN, exclude=["demo__get_weather"])).tools())
    by = {s.name: s for s in specs}
    assert set(by) == {"demo__search_docs", "demo__create_ticket", "demo__delete_records"}
    assert (by["demo__search_docs"].side_effect, by["demo__delete_records"].side_effect, by["demo__create_ticket"].side_effect) == ("read", "irreversible", "write")
    assert by["demo__create_ticket"].annotations["approval"] == "require_approval"
    assert report.servers[0]["protocol"] == "2026-07-28" and {"tool": "demo__get_weather", "reason": "allowlist"} in report.blocked
    assert "ProdAI Atlas" in asyncio.run(by["demo__search_docs"].handler({"query": "atlas"}, {}))


def test_host_untrusted_is_conservative():
    specs, _ = asyncio.run(host_for(effective(CLEAN, trust="community")).tools())
    assert all(s.side_effect == "irreversible" and s.risk == "high" for s in specs)


def test_host_blocks_rug_pull():
    specs, report = asyncio.run(host_for(effective(POISONED, pins=pins_of(CLEAN))).tools())
    assert "demo__search_docs" not in {s.name for s in specs}
    assert {"tool": "demo__search_docs", "reason": "pin_mismatch"} in report.blocked


def test_no_token_passthrough():
    h = upstream_headers({"Authorization": "Bearer user-token", "Cookie": "s=1", "traceparent": "00-ab-cd-01", "X-Custom": "x"})
    assert "Authorization" not in h and "Cookie" not in h and "X-Custom" not in h and h["traceparent"] == "00-ab-cd-01"


def test_selector_ranks_relevant_tools_and_meta_load():
    specs, _ = asyncio.run(host_for(effective(CLEAN)).tools())
    assert mcp_sel.select("사내 문서를 검색해서 요약", specs, k=1) == ["demo__search_docs"]
    assert mcp_sel.select("날씨 알려줘 weather", specs, k=1) == ["demo__get_weather"]
    assert mcp_sel.select("무관한 질문", specs, k=3) == []
    assert len(mcp_sel.select("검색 날씨 티켓 삭제", specs, k=4, budget_tokens=120)) < 4    # 토큰 예산
    search, load = mcp_sel.meta_tools(specs)
    found = asyncio.run(search.handler({"query": "ticket 티켓"}, {}))
    ctx = {}
    asyncio.run(load.handler({"names": [found[0]["name"], "nope"]}, ctx))
    assert found[0]["name"] == "demo__create_ticket" and ctx["loaded_tools"] == ["demo__create_ticket"]


def test_loop_uses_mcp_tool_and_hitl_for_destructive(client):
    from app import db
    specs, _ = asyncio.run(host_for(effective(CLEAN)).tools())
    tools = {t.name: t for t in [*BUILTINS, *specs, *mcp_sel.meta_tools(specs)]}
    e = LoopEngine(LoopStore(db.engine), ScriptGateway(), PlannerClient(None), tools, act_cfg=ActConfig(backoff_base_s=0))
    out = asyncio.run(e.start("acme", "mcp-run-1", 'demo__search_docs({"query":"atlas"}) 로 찾아 요약', tool_names=list(tools)))
    assert out.decision.reason == al_exit.END_TURN and "ProdAI Atlas" in out.result["output"]
    pending = asyncio.run(e.start("acme", "mcp-run-2", 'demo__delete_records({"table":"tmp"}) 정리', tool_names=list(tools)))
    assert pending.decision.reason == al_exit.HITL_PENDING and pending.result["pending_approval"][0]["tool"] == "demo__delete_records"


def test_openapi_to_mcp_tools():
    from mcp_servers.openapi_tools import build
    path = next((p / "apps/manager/internal/api/openapi.json" for p in Path(__file__).resolve().parents
                 if (p / "apps/manager/internal/api/openapi.json").exists()), None)
    if path is None:  # 컨테이너에는 저장소 루트가 없다 — 통합 시험(E7-06)이 실서버로 검증
        pytest.skip("Manager openapi.json 없음 (컨테이너)")
    spec = json.loads(path.read_text())

    async def names():
        async with Client(build(spec, httpx2.AsyncClient(base_url="http://manager:8080"))) as c:
            return {t.name: t.annotations.read_only_hint for t in await c.list_tools()}
    got = asyncio.run(names())
    assert got == {n: True for n in ["listCatalogUnits", "listAgents", "getAgent", "listAgentVersions", "getAgentCard", "searchA2ARegistry", "listNotificationTemplates", "listDeployments", "getDeployment"]}
