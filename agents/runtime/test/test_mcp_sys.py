"""MCP-SYS·MCP-RES — 커넥터 도구(자격증명 주입·결과 비노출)·읽기 전용 ERP·리소스 경계/마스킹/기밀 요약·완성·변경 알림·레지스트리 헬스 서킷·호스트 리소스 캐시."""
import asyncio
import json
import os
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from atlas_agent_core import create_app, events
from units import mcp_reg as reg
from units import mcp_sys
from units.mcp_host import McpHost

ADMIN = {"authorization": "Bearer test-reg-admin"}


class Upstream:
    def __init__(self):
        self.calls, self.down = [], False

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path, req.headers.get("authorization")))
        body = json.loads(req.content) if req.content else {}
        if path.startswith("/policies/"):
            return httpx.Response(200, json={})
        if path == "/v1/guard/check":
            return httpx.Response(200, json={"decision": "transform", "contents": [c.replace("010-1234-5678", "[PHONE]") for c in body["contents"]]})
        if self.down and path.startswith(("/github", "/jira")):
            return httpx.Response(503, json={})
        if path == "/github/repos/atlas/app/issues":
            return httpx.Response(200, json=[{"number": 1, "title": "t"}] if req.method == "GET" else {"number": 2, "title": body["title"]})
        if path == "/slack/chat.postMessage":
            return httpx.Response(200, json={"ok": True, "channel": body["channel"]})
        if path == "/jira/rest/api/3/search":
            return httpx.Response(200, json={"issues": []})
        return httpx.Response(404)


@pytest.fixture
def m(client, monkeypatch):
    from app import db
    u = Upstream()
    monkeypatch.setattr(mcp_sys, "TRANSPORT", httpx.MockTransport(u.handle))
    with TestClient(create_app(mcp_sys.agent, engine=db.engine, public_url="http://mcp-sys:8000")) as c:
        yield c, u


def call(tool, args=None):
    async def go():
        async with Client(mcp_sys.mcp) as c:
            r = await c.call_tool(tool, args or {}, raise_on_error=False)
            return r.is_error, (r.structured_content or {}).get("result", r.structured_content) if not r.is_error else r.content[0].text
    return asyncio.run(go())


def read(uri):
    async def go():
        async with Client(mcp_sys.mcp) as c:
            try:
                return (await c.read_resource(uri))[0].text
            except Exception as exc:  # noqa: BLE001
                return exc
    return asyncio.run(go())


def test_connector_tools_inject_credentials(m):
    _, u = m
    err, issues = call("github_list_issues")
    assert not err and issues[0]["number"] == 1
    assert call("slack_post_message", {"channel": "#ops", "message": "hi"})[1]["ok"] is True
    assert ("GET", "/github/repos/atlas/app/issues", "Bearer fake-github-token") in u.calls
    assert ("POST", "/slack/chat.postMessage", "Bearer fake-slack-token") in u.calls
    assert "fake-github-token" not in json.dumps(issues)
    assert call("health") == (False, {"github": "ok", "jira": "ok"})
    u.down = True
    err, msg = call("health")
    assert err and "업스트림 장애" in msg


def test_erp_read_only_and_named_queries(m):
    err, r = call("db_query", {"name": "orders_by_region", "params": {"region": "KR"}})
    assert not err and [x["id"] for x in r["rows"]] == [1, 3]
    assert call("db_query", {"name": "DROP TABLE erp_orders"})[0]
    with pytest.raises(Exception):  # noqa: B017 — 읽기 전용 연결에서 쓰기는 거부
        mcp_sys.ro_query("DELETE FROM erp_orders", {})
    assert call("db_query", {"name": "order_total"})[1]["rows"][0]["region"] == "KR"


def test_resources_boundary_masking_and_completion(m, monkeypatch):
    orders = json.loads(read("db://erp/erp_orders"))
    assert orders["dataClass"] == "internal" and len(orders["rows"]) >= 3
    cust = json.loads(read("db://erp/erp_customers"))
    assert cust["dataClass"] == "confidential" and "rows" in cust and "010-1234-5678" not in json.dumps(cust)
    assert read("file://docs/public/runbook.md").startswith("# 운영 런북")
    denied = read("file://docs/private/keys.md")
    assert isinstance(denied, Exception) and "경계 밖" in str(denied)
    monkeypatch.setattr(mcp_sys, "project", lambda: "kr-sales")
    kr = json.loads(read("db://erp/erp_orders"))
    assert {r["region"] for r in kr["rows"]} == {"KR"}
    assert isinstance(read("db://erp/erp_customers"), Exception)

    async def comp():
        async with Client(mcp_sys.mcp) as c:
            from mcp.types import ResourceTemplateReference
            return (await c.complete(ResourceTemplateReference(type="ref/resource", uri="db://erp/{table}"), {"name": "table", "value": "erp"})).values
    assert asyncio.run(comp()) == ["erp_orders"]


def test_subscribe_notifies_on_change(m):
    c, _ = m
    # TestClient 는 응답을 다 모은 뒤 돌려주므로 변경은 별도 스레드에서 스트림 도중에 일으킨다
    t = threading.Timer(0.4, lambda: call("erp_create_order", {"region": "JP", "amount": 1, "customer_id": 1}))
    t.start()
    r = c.get("/resources/subscribe", params={"uri": "db://erp/erp_orders", "interval_ms": 100, "max_s": 1})
    t.join()
    seen = [json.loads(line[5:])["method"] for line in r.text.splitlines() if line.startswith("data:")]
    assert seen == ["notifications/resources/subscribed", "notifications/resources/updated"]


def test_registry_health_circuit(client, monkeypatch):
    from app import db
    os.environ["ATLAS_MCP_REG_ADMIN_TOKEN"] = "test-reg-admin"
    state = {"ok": True}

    async def fake_fetch(url):
        return [{"name": "health", "title": None, "description": "h", "input_schema": {"type": "object"}}], "2026-07-28"

    async def fake_probe(url):
        return state["ok"], 5, None if state["ok"] else "down"
    monkeypatch.setattr(reg, "fetch_tools", fake_fetch)
    monkeypatch.setattr(reg, "probe", fake_probe)
    monkeypatch.setattr(reg, "OPEN_AFTER", 2)
    events.sent.clear()
    with TestClient(create_app(reg.agent, engine=db.engine, public_url="http://mcp-reg:8000")) as c:
        sj = {"name": "io.test/health", "version": "1.0.0", "description": "h",
              "packages": [{"registryType": "pypi", "identifier": "x", "transport": {"type": "stdio"}}]}
        assert c.post("/admin/servers", json=sj, headers=ADMIN).status_code in (200, 201)
        c.post("/admin/servers/io.test/health/versions/1.0.0/approve", json={"trust": "community"}, headers=ADMIN)
        c.put("/admin/allowlist/hc", json={"servers": [{"name": "io.test/health", "version": "*"}], "tools": {"include": ["*"], "exclude": []}}, headers=ADMIN)
        eff = c.get("/v0.1/tenants/hc/effective").json()["servers"]
        assert eff[0]["url"].endswith("/servers/io.test--health/mcp")
        c.post("/admin/health/check", headers=ADMIN)
        state["ok"] = False
        for _ in range(2):
            c.post("/admin/health/check", headers=ADMIN)
        h = c.get("/v0.1/servers/io.test/health/versions/1.0.0/health").json()
        assert h["state"] == "open" and c.get("/v0.1/tenants/hc/effective").json()["servers"] == []
        assert any(e[0] == "plt.mcp.sys.health" and e[1]["to"] == "open" for e in events.sent)
        state["ok"] = True
        c.post("/admin/health/check", headers=ADMIN)
        assert c.get("/v0.1/servers/io.test/health/versions/1.0.0/health").json()["state"] == "closed"


def test_host_resource_cache_is_tenant_scoped(m):
    def host(tenant):
        h = McpHost("http://reg", tenant)

        async def eff():
            return {"servers": [{"name": "io.prodai/connectors", "url": mcp_sys.mcp, "trust": "official", "pins": {}}], "tools": {"include": ["*"], "exclude": []}}
        h.effective = eff
        return h

    async def go():
        a, b = host("t-a"), host("t-b")
        r1 = await a.read_resource("io.prodai/connectors", "db://erp/erp_orders")
        r2 = await a.read_resource("io.prodai/connectors", "db://erp/erp_orders")
        r3 = await b.read_resource("io.prodai/connectors", "db://erp/erp_orders")
        await a.close()
        await b.close()
        return r1, r2, r3, a.cache_stats, b.cache_stats
    r1, r2, r3, sa, sb = asyncio.run(go())
    assert (r1["cached"], r2["cached"], r3["cached"]) == (False, True, False) and r1["etag"] == r2["etag"] and r1["ttl_s"] == 30
    assert sa == {"hits": 1, "misses": 1} and sb == {"hits": 0, "misses": 1}
