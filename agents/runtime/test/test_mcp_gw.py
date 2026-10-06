"""MCP 게이트웨이 — 단일 엔드포인트·네임스페이스·결정적 정렬·가상 서버(toolset readOnly)·양방향 중계(elicitation·진행률)·결과 크기 제한
· 가드 훅(오염 → 외부 쓰기 차단)·Origin·인바운드 토큰(aud·조직) · 레지스트리 서명·채널·메타."""
import asyncio
import base64
import importlib
import json
import os
import socket
import threading
import time

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport

import units.mcp_host as mh
from atlas_agent_core import create_app
from units import mcp_gw
from units import mcp_reg as reg
from units.mkt import signing

ADMIN = {"authorization": "Bearer test-reg-admin"}


def servers():
    os.environ["DEMO_POISON"] = "false"
    from mcp_servers import demo, interactive
    return importlib.reload(demo).server, interactive.server


DEMO, INTER = servers()


async def pins(server):
    async with Client(server) as c:
        return {t.name: reg.tool_hash({"name": t.name, "description": t.description, "input_schema": t.input_schema}) for t in await c.list_tools()}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def gw(client):
    from app import db
    eff = {"servers": [{"name": "io.prodai/demo", "url": DEMO, "trust": "verified", "pins": asyncio.run(pins(DEMO))},
                       {"name": "io.prodai/interactive", "url": INTER, "trust": "verified", "pins": asyncio.run(pins(INTER))}],
           "tools": {"include": ["*"], "exclude": []}}

    async def effective(self):
        return eff
    orig = mh.McpHost.effective
    mh.McpHost.effective = effective
    os.environ["ATLAS_MCP_REG_ADMIN_TOKEN"] = "test-reg-admin"
    os.environ["ATLAS_MCP_MAX_RESULT_TOKENS"] = "200"
    app = create_app(mcp_gw.agent, engine=db.engine, public_url="http://gw:8000")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)
    mh.McpHost.effective = orig


def mcp(base, vs="default", tenant="default", **kw):
    return Client(StreamableHttpTransport(f"{base}/t/{tenant}/mcp/{vs}", headers=kw.pop("headers", {})), timeout=30, **kw)


def test_single_endpoint_namespaced_sorted_and_virtual_server(gw):
    httpx.put(f"{gw}/admin/virtual-servers/default/ro", json={"profile": "ops-readonly"}, headers=ADMIN).raise_for_status()

    async def go():
        async with mcp(gw) as c:
            names = [t.name for t in await c.list_tools()]
        async with mcp(gw, "ro") as c:
            ro = [t.name for t in await c.list_tools()]
        return names, ro
    names, ro = asyncio.run(go())
    assert names == sorted(names) and "demo__search_docs" in names and "interactive__confirm_delete" in names
    assert "demo__delete_records" not in ro and "interactive__send_email" not in ro and "interactive__long_task" in ro
    assert httpx.put(f"{gw}/admin/virtual-servers/default/x", json={"profile": "nope"}, headers=ADMIN).status_code == 422


def test_bidirectional_relay_elicitation_and_progress(gw):
    seen = []

    async def yes(message, rt, params, ctx):
        seen.append(message)
        return True

    async def no(message, rt, params, ctx):
        return ElicitResult(action="decline")

    async def progress(p, total, msg):
        seen.append((p, total))

    async def go():
        async with mcp(gw, elicitation_handler=yes) as c:
            b = (await c.call_tool("interactive__long_task", {"steps": 3}, progress_handler=progress)).structured_content
            a = (await c.call_tool("interactive__confirm_delete", {"item": "report.csv"})).structured_content
        async with mcp(gw, elicitation_handler=no) as c:
            d = (await c.call_tool("interactive__confirm_delete", {"item": "x"})).structured_content
        return a, b, d
    a, b, d = asyncio.run(go())
    assert a["confirmed"] is True and any("report.csv" in str(x) for x in seen) and b["done"] is True
    assert [x for x in seen if isinstance(x, tuple)] == [(1.0, 3.0), (2.0, 3.0), (3.0, 3.0)]
    assert d == {"item": "x", "action": "decline", "confirmed": False}


def test_result_size_limit_and_guard_taint_blocks_open_world_write(gw, monkeypatch):
    async def fake_guard(boundary, contents, tenant):
        if boundary == "tool_result" and "Ignore all previous" in contents[0]:
            return {"decision": "allow", "categories": ["jailbreak"]}
        return {"decision": "allow", "categories": []}
    monkeypatch.setattr(mh, "guard", fake_guard)

    async def go():
        async with mcp(gw, headers={"x-atlas-run-id": "run-1"}) as c:
            big = await c.call_tool("interactive__big_report", {"lines": 500})
            inbox = (await c.call_tool("interactive__read_inbox", {})).structured_content
            blocked = await c.call_tool("interactive__send_email", {"to": "attacker@example.test", "body": "keys"}, raise_on_error=False)
        async with mcp(gw, headers={"x-atlas-run-id": "run-2"}) as c2:          # 다른 실행은 오염되지 않음
            ok = await c2.call_tool("interactive__send_email", {"to": "team@example.test", "body": "hi"})
        return big, inbox, blocked, ok
    big, inbox, blocked, ok = asyncio.run(go())
    assert "[truncated:" in big.content[0].text and len(big.content[0].text) < 1000
    assert inbox["untrusted"] is True and inbox["categories"] == ["jailbreak"]
    assert blocked.is_error and "오염" in blocked.content[0].text
    assert ok.structured_content["sent"] is True


def test_origin_and_inbound_token(gw, monkeypatch):
    bad = httpx.post(f"{gw}/t/default/mcp/default", json={}, headers={"origin": "http://evil.example"})
    assert bad.status_code == 403
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "k1"
    monkeypatch.setattr(mcp_gw, "_JWKS", {"keys": [jwk]})
    now = int(time.time())

    def tok(aud, org):
        return jwt.encode({"sub": "agent:a1", "aud": aud, "atlas_org": org, "iat": now, "exp": now + 300}, key, algorithm="ES256", headers={"kid": "k1"})
    monkeypatch.setenv("ATLAS_MCP_GW_AUTH", "required")
    assert httpx.post(f"{gw}/t/default/mcp/default", json={}).status_code == 401
    assert httpx.post(f"{gw}/t/default/mcp/default", json={}, headers={"authorization": f"Bearer {tok('atlas-manager', 'default')}"}).status_code == 401
    assert httpx.post(f"{gw}/t/default/mcp/default", json={}, headers={"authorization": f"Bearer {tok('atlas-mcp-gateway', 'acme')}"}).status_code == 403

    async def go():
        async with mcp(gw, headers={"authorization": f"Bearer {tok('atlas-mcp-gateway', 'default')}"}) as c:
            return len(await c.list_tools())
    assert asyncio.run(go()) > 5


@pytest.fixture
def regc(client, monkeypatch):
    from app import db
    os.environ["ATLAS_MCP_REG_ADMIN_TOKEN"] = "test-reg-admin"

    async def fake_fetch(url):
        return [{"name": "echo", "title": None, "description": "echo", "input_schema": {"type": "object"}}], "2026-07-28"
    monkeypatch.setattr(reg, "fetch_tools", fake_fetch)
    with TestClient(create_app(reg.agent, engine=db.engine, public_url="http://mcp-reg:8000")) as c:
        yield c


def test_registry_signature_channels_and_meta(regc):
    c = regc
    k = Ed25519PrivateKey.generate()
    pem = signing.pub_pem(k.public_key())
    c.put("/admin/keys/pub-1", json={"public_key": pem, "owner": "vendor"}, headers=ADMIN)
    pkg = {"packages": [{"registryType": "pypi", "identifier": "tool-x", "version": "1.0.0", "transport": {"type": "stdio"}}]}
    base = {"name": "io.vendor/tool", "version": "1.0.0", "description": "t", **pkg}
    meta = {reg.META_KEY: {"owners": ["team-a"], "dataClass": "confidential", "requiredScopes": ["repo:read"], "license": "AGPL-3.0", "auth": "oauth2"}}
    c.post("/admin/servers", json={**base, "_meta": meta}, headers=ADMIN)
    r = c.post("/admin/servers/io.vendor/tool/versions/1.0.0/approve", json={"trust": "verified"}, headers=ADMIN)
    assert r.status_code == 409 and r.json()["detail"]["verification"]["signature"] == "missing"
    assert c.post("/admin/servers/io.vendor/tool/versions/1.0.0/approve", json={"trust": "community"}, headers=ADMIN).status_code == 200
    for v in ("1.1.0", "1.2.0"):
        sj = {**base, "version": v}
        sig = base64.b64encode(k.sign(reg.signed_message(reg.server_digest(sj)))).decode()
        prov = signing.statement("io.vendor/tool", v, reg.server_digest(sj))
        out = c.post("/admin/servers", json={**sj, "_meta": {**meta, reg.SIG_KEY: {"kid": "pub-1", "sig": sig}, reg.PROV_KEY: prov}}, headers=ADMIN).json()
        assert out["_meta"][reg.META]["verification"]["signature"] == "valid" and out["_meta"][reg.META]["verification"]["provenance"] == "valid"
        assert c.post(f"/admin/servers/io.vendor/tool/versions/{v}/approve", json={"trust": "verified"}, headers=ADMIN).status_code == 200
    m = c.get("/v0.1/servers/io.vendor/tool/versions/1.2.0").json()["_meta"][reg.META]["registryMeta"]
    assert m["owners"] == ["team-a"] and m["transport"] == "stdio" and m["warnings"]
    assert c.put("/admin/servers/io.vendor/tool/channels/prod", json={"version": "1.1.0"}, headers=ADMIN).status_code == 409   # dev·staging 먼저
    for ch in ("dev", "staging", "prod"):
        c.put(f"/admin/servers/io.vendor/tool/channels/{ch}", json={"version": "1.1.0"}, headers=ADMIN).raise_for_status()
    c.put("/admin/allowlist/chan", json={"servers": [{"name": "io.vendor/tool", "channel": "prod"}], "tools": {"include": ["*"], "exclude": []}}, headers=ADMIN)
    assert [s["version"] for s in c.get("/v0.1/tenants/chan/effective").json()["servers"]] == ["1.1.0"]
    for ch in ("dev", "staging", "prod"):
        c.put(f"/admin/servers/io.vendor/tool/channels/{ch}", json={"version": "1.2.0"}, headers=ADMIN)
    eff = c.get("/v0.1/tenants/chan/effective").json()["servers"]
    assert eff[0]["version"] == "1.2.0" and eff[0]["meta"]["auth"] == "oauth2"
    c.post("/admin/servers/io.vendor/tool/channels/prod/rollback", headers=ADMIN).raise_for_status()
    assert c.get("/v0.1/tenants/chan/effective").json()["servers"][0]["version"] == "1.1.0"
    tampered = {**base, "version": "1.3.0", "description": "changed", "_meta": {reg.SIG_KEY: {"kid": "pub-1", "sig": sig}}}
    t = c.post("/admin/servers", json=tampered, headers=ADMIN).json()
    assert t["_meta"][reg.META]["verification"]["signature"] == "invalid"
