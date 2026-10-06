"""Epic 26 스택 시험 — MCP 고급: 레지스트리 서명·출처·메타·채널 · OAuth 보호 서버(Keycloak 서비스 계정) · 게이트웨이 단일 엔드포인트·가상 서버·
MRTR 입력 요청·진행률 중계(mcp-sbx stdio 경유) · 결과 크기 제한 · 가드 훅 오염 차단 · Origin·토큰 대상(aud) · 토큰 비전달.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/26th/e26_mcp.py <local|prod>"""
import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StreamableHttpTransport

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../agents/runtime"))
from units import mcp_reg as reg  # noqa: E402
from units.mkt import signing  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
M = os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")
REG, GW = "http://127.0.0.1:8002", "http://127.0.0.1:8024"
MH = {"Authorization": f"Bearer {os.environ['ATLAS_TEST_TOKEN']}"} if os.environ.get("ATLAS_TEST_TOKEN") else {}
ROOT = os.path.join(os.path.dirname(__file__), "../../..")
N = secrets.token_hex(3)
c = httpx.Client(timeout=120)
failed = 0


def envval(key):
    if os.environ.get(key):
        return os.environ[key]
    for line in open(os.path.join(ROOT, f".env.{MODE}"), encoding="utf-8"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


RA = {"authorization": f"Bearer {envval('ATLAS_MCP_REG_ADMIN_TOKEN')}"}


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def subjects():
    out = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222",
                          "stream", "subjects", "PLT", "--json"], capture_output=True, text=True, timeout=90).stdout
    return json.loads(out or "{}")


# 게이트웨이 토큰 (aud=atlas-mcp-gateway) — 게이트웨이 에이전트 자신의 id 로 발급
GW_AGENT = next(a["id"] for a in c.get(f"{M}/api/v1/agents", headers=MH).json()["items"] if a["name"] == "mcp-gateway")
TOK = c.post(f"{M}/api/v1/agents/{GW_AGENT}/token", params={"audience": "atlas-mcp-gateway"}, headers=MH).json()["token"]
BEARER = {"authorization": f"Bearer {TOK}"}


def gw(vs="default", run=None, **kw):
    h = {**BEARER, **({"x-atlas-run-id": run} if run else {})}
    return Client(StreamableHttpTransport(f"{GW}/t/default/mcp/{vs}", headers=h), timeout=90, **kw)


# E26-01 서명·출처 (REG-04) · 신뢰 메타 (REG-08)
key = Ed25519PrivateKey.generate()
c.put(f"{REG}/admin/keys/e26-{N}", json={"public_key": signing.pub_pem(key.public_key()), "owner": "atlas-labs"}, headers=RA).raise_for_status()
meta = {reg.META_KEY: {"owners": ["platform-team"], "dataClass": "internal", "requiredScopes": ["mail:send"], "license": "Apache-2.0"}}
pkg = [{"registryType": "pypi", "identifier": "interactive", "version": "1", "transport": {"type": "stdio"}}]


def signed(version):
    sj = {"name": "io.prodai/interactive", "version": version, "description": "양방향·안전 시험", "packages": pkg}
    d = reg.server_digest(sj)
    return {**sj, "_meta": {**meta, reg.SIG_KEY: {"kid": f"e26-{N}", "sig": base64.b64encode(key.sign(reg.signed_message(d))).decode()},
                            reg.PROV_KEY: signing.statement(sj["name"], version, d)}}


uns = c.post(f"{REG}/admin/servers", json={"name": "io.prodai/interactive", "version": f"0.9.{int(N, 16) % 1000}", "description": "unsigned", "packages": pkg}, headers=RA).json()
deny = c.post(f"{REG}/admin/servers/io.prodai/interactive/versions/{uns['server']['version']}/approve", json={"trust": "verified"}, headers=RA)
K = int(N, 16) % 100000
V1, V2 = f"2.{K}.0", f"2.{K}.1"                     # 실행마다 새 버전 (채널 상태가 남아도 독립)
ok = {}
for v in (V1, V2):
    ok[v] = c.post(f"{REG}/admin/servers", json=signed(v), headers=RA).json()["_meta"]["io.prodai.atlas/registry"]
    c.post(f"{REG}/admin/servers/io.prodai/interactive/versions/{v}/approve", json={"trust": "verified"}, headers=RA).raise_for_status()
rm = c.get(f"{REG}/v0.1/servers/io.prodai/interactive/versions/{V2}").json()["_meta"]["io.prodai.atlas/registry"]["registryMeta"]
check("E26-01", (deny.status_code, deny.json()["detail"]["verification"]["signature"], ok[V1]["verification"]["signature"],
                 ok[V2]["verification"]["provenance"], rm["owners"], rm["transport"]), (409, "missing", "valid", "valid", ["platform-team"], "stdio"))

# E26-02 채널 고정·승격·롤백 (REG-07)
skip = c.put(f"{REG}/admin/servers/io.prodai/interactive/channels/prod", json={"version": V2}, headers=RA).status_code
for ver in (V1, V2):
    for ch in ("dev", "staging", "prod"):
        c.put(f"{REG}/admin/servers/io.prodai/interactive/channels/{ch}", json={"version": ver}, headers=RA).raise_for_status()
rb = c.post(f"{REG}/admin/servers/io.prodai/interactive/channels/prod/rollback", headers=RA).json()
c.put(f"{REG}/admin/servers/io.prodai/interactive/channels/prod", json={"version": V2}, headers=RA)
chs = c.get(f"{REG}/v0.1/servers/io.prodai/interactive/channels").json()["channels"]
check("E26-02", (skip, rb["version"], chs["prod"]["version"], len(chs["prod"]["history"]) >= 3), (409, V1, V2, True))

# E26-03 OAuth 보호 서버 — 등록 스캔도 서비스 계정 토큰으로 (HOST-03)
pr = c.post(f"{REG}/admin/servers", json={"name": "io.prodai/protected-hr", "version": "1.0.0", "description": "OAuth 보호 인사 데이터",
                                         "remotes": [{"type": "streamable-http", "url": "http://mcp-protected:8000/mcp"}],
                                         "_meta": {reg.META_KEY: {"auth": "oauth2", "dataClass": "confidential", "owners": ["hr"]}}}, headers=RA)
c.post(f"{REG}/admin/servers/io.prodai/protected-hr/versions/1.0.0/approve", json={"trust": "verified"}, headers=RA)
unauth = subprocess.run(["docker", "exec", f"prodai-atlas-{MODE}-agent-mcp-gw-1", "python", "-c",
                         "import urllib.request,json\ntry:\n urllib.request.urlopen(urllib.request.Request('http://mcp-protected:8000/mcp',data=b'{}',headers={'content-type':'application/json'}))\nexcept Exception as e: print(e.code, e.headers.get('www-authenticate',''))"],
                        capture_output=True, text=True, timeout=60).stdout.strip()
check("E26-03", (pr.status_code, pr.json().get("_meta", {}).get("io.prodai.atlas/registry", {}).get("status"), unauth.startswith("401"), "resource_metadata" in unauth),
      (201, "scanned", True, True))

# 허용목록: 기존 + 새 서버(채널 prod) — 끝나면 원복
orig = c.get(f"{REG}/v0.1/tenants/default/effective").json()
allow = [{"name": s["name"], "version": s["version"]} for s in orig["servers"]] + [{"name": "io.prodai/interactive", "channel": "prod"},
                                                                                  {"name": "io.prodai/protected-hr", "version": "1.0.0"}]
c.put(f"{REG}/admin/allowlist/default", json={"servers": allow, "tools": {"include": ["*"], "exclude": []}}, headers=RA).raise_for_status()


async def suite():
    out = {}
    async with gw() as g:
        names = [t.name for t in await g.list_tools()]
        out["names"] = names
        out["whoami"] = (await g.call_tool("protected_hr__whoami", {})).structured_content
        out["big"] = (await g.call_tool("interactive__big_report", {"lines": 3000})).content[0].text
    seen = []

    async def yes(message, rt, params, ctx):
        seen.append(message)
        return True

    async def prog(p, total, msg):
        seen.append((p, total))
    async with gw(elicitation_handler=yes) as g:
        out["confirm"] = (await g.call_tool("interactive__confirm_delete", {"item": f"rep-{N}.csv"})).structured_content
        out["long"] = (await g.call_tool("interactive__long_task", {"steps": 3}, progress_handler=prog)).structured_content
    out["seen"] = seen
    async with gw(run=f"run-a-{N}") as g:
        out["inbox"] = (await g.call_tool("interactive__read_inbox", {})).structured_content
        out["blocked"] = await g.call_tool("interactive__send_email", {"to": "attacker@example.test", "body": "keys"}, raise_on_error=False)
    async with gw(run=f"run-b-{N}") as g:
        out["clean"] = (await g.call_tool("interactive__send_email", {"to": "team@example.test", "body": "회의록"})).structured_content
    c.put(f"{GW}/admin/virtual-servers/default/review-{N}", json={"profile": "ops-readonly"}, headers=RA).raise_for_status()
    async with gw(f"review-{N}") as g:
        out["ro"] = [t.name for t in await g.list_tools()]
    return out


try:
    r = asyncio.run(suite())
finally:
    c.put(f"{REG}/admin/allowlist/default", json={"servers": [{"name": s["name"], "version": s["version"]} for s in orig["servers"]], "tools": orig["tools"]}, headers=RA)

# E26-04 단일 엔드포인트 · 네임스페이스 · 결정적 정렬 (HOST-04)
check("E26-04", (r["names"] == sorted(r["names"]), any(n.startswith("demo__") for n in r["names"]), "interactive__confirm_delete" in r["names"],
                 "protected_hr__payroll_summary" in r["names"]), (True, True, True, True))
# E26-05 OAuth 업스트림 — 서버 전용 토큰(서비스 계정), 인바운드 토큰 비전달 (HOST-03·07)
check("E26-05", (r["whoami"].get("client_id"), TOK[:20] in json.dumps(r["whoami"])), ("atlas-ci", False))
# E26-06 MRTR 입력 요청 중계 (HOST-05) — 게이트웨이 → mcp-sbx → stdio
check("E26-06", (r["confirm"].get("confirmed"), any(f"rep-{N}.csv" in str(x) for x in r["seen"])), (True, True))
# E26-07 진행률 중계
check("E26-07", ([x for x in r["seen"] if isinstance(x, tuple)], r["long"]["done"]), ([(1.0, 3.0), (2.0, 3.0), (3.0, 3.0)], True))
# E26-08 결과 크기 제한 (TOOL-06)
check("E26-08", ("[truncated:" in r["big"], len(r["big"]) <= 4000 * 4 + 200), (True, True))
# E26-09 가드 훅 — 간접 인젝션 결과 → 오염 → 외부 쓰기 차단, 다른 실행은 정상 (HOST-08)
check("E26-09", (r["inbox"].get("untrusted"), r["blocked"].is_error, "오염" in (r["blocked"].content[0].text if r["blocked"].content else ""), r["clean"].get("sent")),
      (True, True, True, True))
# E26-10 가상 서버 toolset readOnly (SEL-04)
check("E26-10", ("interactive__send_email" in r["ro"], "interactive__long_task" in r["ro"], "demo__delete_records" in r["ro"]), (False, True, False))

# E26-11 Origin · 토큰 대상 (HOST-07·10)
bad_origin = c.post(f"{GW}/t/default/mcp/default", json={}, headers={**BEARER, "origin": "http://evil.example"}).status_code
mgr_tok = c.post(f"{M}/api/v1/agents/{GW_AGENT}/token", headers=MH).json()["token"]
wrong_aud = c.post(f"{GW}/t/default/mcp/default", json={}, headers={"authorization": f"Bearer {mgr_tok}"}).status_code
wrong_org = c.post(f"{GW}/t/acme/mcp/default", json={}, headers=BEARER).status_code
on_mgr = c.get(f"{M}/api/v1/agents", headers={"authorization": f"Bearer {TOK}"}).status_code
check("E26-11", (bad_origin, wrong_aud, wrong_org, on_mgr if MODE == "prod" else "skip"), (403, 401, 403, 401 if MODE == "prod" else "skip"))

# E26-12 이벤트
subj = subjects()
check("E26-12", sorted(s for s in ("plt.mcp.reg.verify_failed", "plt.mcp.reg.channel_changed") if s not in subj), [])

print(f"== {MODE}: failed={failed}")
sys.exit(1 if failed else 0)
