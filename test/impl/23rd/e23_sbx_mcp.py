"""Epic 23 스택 시험 — X-SBX 샌드박스·egress 프록시·MCP-SYS 커넥터·MCP-RES 리소스·mcp-sbx(stdio 격리)·MCP-REG 헬스 서킷·MCP-HOST 리소스 캐시.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/23rd/e23_sbx_mcp.py <local|prod>"""
import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
SBX, SYS, REG = "http://127.0.0.1:8019", "http://127.0.0.1:8020", "http://127.0.0.1:8002"
M = os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")
MH = {"Authorization": f"Bearer {os.environ['ATLAS_TEST_TOKEN']}"} if os.environ.get("ATLAS_TEST_TOKEN") else {}
ROOT = os.path.join(os.path.dirname(__file__), "../../..")
c = httpx.Client(timeout=120)
failed = 0


def envval(key):
    if os.environ.get(key):
        return os.environ[key]
    for line in open(os.path.join(ROOT, f".env.{MODE}"), encoding="utf-8"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


REG_ADMIN = {"authorization": f"Bearer {envval('ATLAS_MCP_REG_ADMIN_TOKEN')}"}


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def dexec(svc, code):
    r = subprocess.run(["docker", "exec", f"prodai-atlas-{MODE}-{svc}-1", "python", "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=180)
    if r.returncode:
        print(r.stderr[-1500:], file=sys.stderr)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def run(**body):
    return c.post(f"{SBX}/v1/runs", json=body).json()


def mcp(project="default"):
    return Client(StreamableHttpTransport(f"{SYS}/connectors/mcp", headers={"x-atlas-project": project}), timeout=60)


async def tool(name, args=None, project="default"):
    async with mcp(project) as cl:
        r = await cl.call_tool(name, args or {}, raise_on_error=False)
        return r.is_error, (r.structured_content or {}).get("result", r.structured_content) if not r.is_error else r.content[0].text


async def resource(uri, project="default"):
    async with mcp(project) as cl:
        try:
            return (await cl.read_resource(uri))[0].text
        except Exception as exc:  # noqa: BLE001
            return f"ERR {exc}"


# 커넥터 자격증명 — Manager 비밀 중개로 (시험 값 = 가짜 업스트림 토큰)
for sysname in ("github", "slack", "jira"):
    c.put(f"{M}/api/v1/orgs/default/secrets/{sysname}-token", json={"value": f"fake-{sysname}-token"}, headers=MH).raise_for_status()

# E23-01 Python 실행 · out/ 산출물 · 실행 기록 (X-SBX-01·05)
r1 = run(lang="python", code="import os\nos.makedirs('out', exist_ok=True)\nopen('out/report.txt','w').write('total=42')\nprint(sum(range(10)))")
rec = c.get(f"{SBX}/v1/runs/{r1['run_id']}").json()
check("E23-01", (r1["status"], r1["stdout"].strip(), [a["name"] for a in r1["artifacts"]], r1["artifacts"][0]["text"], rec["isolation"], len(rec["code_sha256"])),
      ("ok", "45", ["report.txt"], "total=42", "container", 64))

# E23-02 JavaScript · 셸 (X-SBX-01)
js = run(lang="javascript", code="console.log([1,2,3].map(x => x * 2).join(','))")
sh = run(lang="shell", code="echo hi-$((6*7)); exit 3")
check("E23-02", (js["status"], js["stdout"].strip(), sh["status"], sh["exit_code"], sh["stdout"].strip()), ("ok", "2,4,6", "error", 3, "hi-42"))

# E23-03 기본 네트워크 차단 → 허용목록 송신 · 허용 밖·메타데이터 차단 (X-SBX-02)
FETCH = "import urllib.request,sys\nfor u in sys.argv[1:] or {urls}:\n    try:\n        print('OK', urllib.request.urlopen(u, timeout=5).status)\n    except Exception as e:\n        print('ERR', type(e).__name__)"
none = run(lang="python", code=FETCH.format(urls=["http://fake-upstream:8000/health"]))
eg = run(lang="python", code=FETCH.format(urls=["http://fake-upstream:8000/health", "http://example.com/", "http://169.254.169.254/latest/meta-data"]),
         egress={"allow": ["fake-upstream", "169.254.169.254"]})
dec = {(d["host"], d["decision"], d["reason"]) for d in eg["egress"]["decisions"]}
check("E23-03", (none["stdout"].split()[0], eg["stdout"].split("\n")[0].strip(), eg["stdout"].count("ERR"),
                 ("fake-upstream", "allow", "allowed") in dec, ("example.com", "deny", "not_in_allowlist") in dec, ("169.254.169.254", "deny", "metadata_address") in dec),
      ("ERR", "OK 200", 2, True, True, True))

# E23-04 자원 상한 · 상한 초과 거부 · 격리 수준 (X-SBX-02·03)
to = run(lang="python", code="while True: pass", timeout_s=2)
mem = run(lang="python", code="x = bytearray(400 * 1024 * 1024)", limits={"memory_mb": 128})
over = c.post(f"{SBX}/v1/runs", json={"code": "1", "timeout_s": 9999})
iso = c.post(f"{SBX}/v1/runs", json={"code": "1", "isolation": "gvisor"})
check("E23-04", (to["status"], to["timed_out"], mem["status"], over.status_code, iso.status_code, c.get(f"{SBX}/v1/isolation").json()["available"]),
      ("limit_exceeded", True, "limit_exceeded", 422, 422, ["container"]))

# E23-05 산출물 비밀 스캔 · PII (X-SBX-06)
leak = run(lang="python", code="print('AKIA' + 'Q' * 16)\nprint('call 010-1234-5678')")
check("E23-05", ("[REDACTED:aws_access_key]" in leak["stdout"], "AKIAQQ" in leak["stdout"], "010-1234-5678" in leak["stdout"],
                 "aws_access_key" in {f["type"] for f in leak["findings"]}), (True, False, False, True))

# E23-06 A2A skill sbx.run_code
t = c.post(f"{SBX}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"metadata": {"skill": "sbx.run_code"}, "message": {
    "messageId": "m1", "role": "ROLE_USER", "parts": [{"data": {"lang": "python", "code": "print(6*7)"}}]}}}).json()["result"]["task"]
art = json.dumps(t.get("artifacts", []), ensure_ascii=False)
check("E23-06", (t["status"]["state"], '"stdout": "42' in art or "42\\n" in art), ("TASK_STATE_COMPLETED", True))

# E23-07 커넥터 도구 — 자격증명 주입·결과 비노출 (MCP-SYS-01·02)
e1, issues0 = asyncio.run(tool("github_list_issues"))
e2, made = asyncio.run(tool("github_create_issue", {"title": f"e23-{time.time_ns()}"}))
e3, sl = asyncio.run(tool("slack_post_message", {"channel": "#ops", "message": "배포 완료"}))
calls = json.loads(dexec("agent-mcp-sys", "import urllib.request;print(urllib.request.urlopen('http://fake-upstream:8000/calls').read().decode())"))["items"]
check("E23-07", (e1, e2, e3, made["title"].startswith("e23-"), sl["ok"], all(x["auth"] for x in calls[-3:]), "fake-github-token" in json.dumps([issues0, made, sl])),
      (False, False, False, True, True, True, False))

# E23-08 web_fetch egress — 허용목록·메타데이터 차단 (MCP-SYS-04)
_, w1 = asyncio.run(tool("web_fetch", {"url": "http://fake-upstream:8000/docs/faq.html"}))
_, w2 = asyncio.run(tool("web_fetch", {"url": "http://example.com/"}))
_, w3 = asyncio.run(tool("web_fetch", {"url": "http://169.254.169.254/latest/meta-data"}))
check("E23-08", (w1.get("status"), w2.get("blocked"), w3.get("blocked")), (200, True, True))

# E23-09 ERP DB — 이름 질의·읽기 전용·행 필터 경계 (MCP-SYS-05) · prod 는 SELECT 전용 역할
_, q = asyncio.run(tool("db_query", {"name": "orders_by_region", "params": {"region": "KR"}}))
bad, _ = asyncio.run(tool("db_query", {"name": "DELETE FROM erp_orders"}))
_, kr = asyncio.run(tool("db_query", {"name": "order_total"}, project="kr-sales"))
ro = "n/a"
if MODE == "prod":
    ro = dexec("agent-mcp-sys", """
        import os, sqlalchemy as sa
        e = sa.create_engine(os.environ['DATABASE_URL'])
        with e.connect() as c:
            print(c.execute(sa.text("SELECT has_table_privilege('atlas_erp_ro','erp_orders','SELECT'), has_table_privilege('atlas_erp_ro','erp_orders','INSERT')")).one())""")
check("E23-09", ({r["region"] for r in q["rows"]}, bad, q["truncated"], ro if MODE == "prod" else "n/a"),
      ({"KR"}, True, False, "(True, False)" if MODE == "prod" else "n/a"))

# E23-10 리소스 — 템플릿·기밀 요약·PII 마스킹·경계 -32002·완성 (MCP-RES-01·02·05·06)


async def res_suite():
    async with mcp() as cl:
        tpls = sorted(t.uriTemplate for t in await cl.list_resource_templates())
        from mcp.types import ResourceTemplateReference
        comp = (await cl.complete(ResourceTemplateReference(type="ref/resource", uri="db://erp/{table}"), {"name": "table", "value": "erp_c"})).values
    return tpls, comp
tpls, comp = asyncio.run(res_suite())
orders = json.loads(asyncio.run(resource("db://erp/erp_orders")))
cust = json.loads(asyncio.run(resource("db://erp/erp_customers")))
priv = asyncio.run(resource("file://docs/private/keys.md"))
krcust = asyncio.run(resource("db://erp/erp_customers", project="kr-sales"))
krord = json.loads(asyncio.run(resource("db://erp/erp_orders", project="kr-sales")))
check("E23-10", (tpls, comp, orders["dataClass"], cust["dataClass"], "010-" in json.dumps(cust, ensure_ascii=False), "sha256" in cust["sample"][0],
                 priv.startswith("ERR") and "경계 밖" in priv, krcust.startswith("ERR"), {r["region"] for r in krord["rows"]}),
      (["db://erp/{table}", "file://docs/{path*}"], ["erp_customers"], "internal", "confidential", False, True, True, True, {"KR"}))

# E23-11 변경 알림 — resources/subscribe SSE → notifications/resources/updated (MCP-RES-03)
seen = []


def listen():
    with httpx.stream("GET", f"{SYS}/resources/subscribe", params={"uri": "db://erp/erp_orders", "interval_ms": 200, "max_s": 8}, timeout=20) as s:
        for line in s.iter_lines():
            if line.startswith("data:"):
                seen.append(json.loads(line[5:])["method"])
                if len(seen) == 2:
                    return
th = threading.Thread(target=listen)
th.start()
time.sleep(1.0)
ce, newo = asyncio.run(tool("erp_create_order", {"region": "JP", "amount": 5, "customer_id": 1}))
th.join(15)
check("E23-11", (ce, seen), (False, ["notifications/resources/subscribed", "notifications/resources/updated"]))

# E23-12 레지스트리 — 커넥터(공식)·stdio 데모(격리 브리지) 등록·승인·허용목록 (MCP-SYS-03 · X-SBX-07)
seed = subprocess.run(["bash", "test/impl/14th/dc.sh", MODE, "exec", "-T", "agent-mcp-reg", "python", "-m", "units.mcp_reg.seed", "--systems"], cwd=ROOT, capture_output=True, text=True, timeout=300)
print(seed.stdout.strip()[-800:], flush=True)
eff = {s["name"]: s for s in c.get(f"{REG}/v0.1/tenants/default/effective").json()["servers"]}
conn, stdio = eff.get("io.prodai/connectors", {}), eff.get("io.prodai/stdio-echo", {})
check("E23-12", (conn.get("trust"), conn.get("url"), stdio.get("trust"), stdio.get("url"), "health" in conn.get("pins", {})),
      ("official", "http://agent-mcp-sys:8000/connectors/mcp", "community", "http://mcp-sbx:8000/servers/io.prodai--stdio-echo/mcp", True))

# E23-13 stdio 서버 격리 실행 — 환경 비밀 비노출·읽기 전용 FS·프로필 egress (X-SBX-07)
iso_out = dexec("agent-al-run", """
    import asyncio, json
    from fastmcp import Client
    async def m():
        async with Client("http://mcp-sbx:8000/servers/io.prodai--stdio-echo/mcp", timeout=60) as c:
            w = (await c.call_tool("whoami", {})).structured_content
            ok = (await c.call_tool("fetch", {"url": "http://fake-upstream:8000/health"})).structured_content
            no = (await c.call_tool("fetch", {"url": "http://example.com/"})).structured_content
            wr = (await c.call_tool("write_file", {"path": "/app/pwned.txt", "text": "x"})).structured_content
            print(json.dumps([w["uid"], [k for k in w["env_keys"] if "TOKEN" in k or "KEY" in k or "SECRET" in k], ok["ok"], no["ok"], wr["ok"]]))
    asyncio.run(m())""")
uid, leaked, f_ok, f_no, wr_ok = json.loads(iso_out or "[0, [\"?\"], false, true, true]")
check("E23-13", (uid != 0, leaked, f_ok, f_no, wr_ok), (True, [], True, False, False))

# E23-14 헬스·SLA 서킷 — 업스트림 장애 → open(노출 제외)·이벤트 → 복구 → closed (MCP-SYS-06)
H = f"{REG}/v0.1/servers/io.prodai/connectors/versions/1.0.0/health"
c.post(f"{REG}/admin/health/check", headers=REG_ADMIN)
h0 = c.get(H).json()
dexec("agent-mcp-sys", "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://fake-upstream:8000/admin/down', data=b'{\"down\": true}', method='POST'))")
for _ in range(3):
    c.post(f"{REG}/admin/health/check", headers=REG_ADMIN)
h1 = c.get(H).json()
hidden = "io.prodai/connectors" not in {s["name"] for s in c.get(f"{REG}/v0.1/tenants/default/effective").json()["servers"]}
dexec("agent-mcp-sys", "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://fake-upstream:8000/admin/down', data=b'{\"down\": false}', method='POST'))")
c.post(f"{REG}/admin/health/check", headers=REG_ADMIN)
h2 = c.get(H).json()
subj = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222",
                       "stream", "subjects", "PLT", "--json"], capture_output=True, text=True, timeout=90).stdout
subjects = json.loads(subj or "{}")
check("E23-14", (h0["state"], h1["state"], h1["failures"] >= 3, hidden, h2["state"], "plt.mcp.sys.health" in subjects, "plt.sbx.limit_exceeded" in subjects),
      ("closed", "open", True, True, "closed", True, True))

# E23-15 호스트 리소스 캐시 — 테넌트·프로젝트 키·etag·ttlMs (MCP-RES-04) · 경계는 호스트 경유에도 유지
host_out = dexec("agent-al-run", """
    import asyncio, json
    from units.mcp_host import McpHost
    async def m():
        a = McpHost("http://agent-mcp-reg:8000", "default")
        r1 = await a.read_resource("io.prodai/connectors", "db://erp/erp_orders")
        r2 = await a.read_resource("io.prodai/connectors", "db://erp/erp_orders")
        k = McpHost("http://agent-mcp-reg:8000", "default", project="kr-sales")
        r3 = await k.read_resource("io.prodai/connectors", "db://erp/erp_orders")
        try:
            await k.read_resource("io.prodai/connectors", "db://erp/erp_customers")
            d = "allowed"
        except Exception as e:
            d = "denied" if "경계 밖" in str(e) else str(e)[:80]
        print(json.dumps([r1["cached"], r2["cached"], r1["etag"] == r2["etag"], r1["ttl_s"], r3["cached"], sorted({x["region"] for x in json.loads(r3["text"])["rows"]}), d, a.cache_stats]))
        await a.close(); await k.close()
    asyncio.run(m())""")
check("E23-15", json.loads(host_out or "[]"), [False, True, True, 30.0, False, ["KR"], "denied", {"hits": 1, "misses": 1}])

print(f"== {MODE}: failed={failed}")
sys.exit(1 if failed else 0)
