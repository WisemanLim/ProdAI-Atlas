#!/usr/bin/env python3
"""Epic 7 MCP E2E (E7-03~07·10) — 사용: e7_mcp.py <local|prod> [manager] [registry] [al_run]"""
import json
import secrets
import subprocess
import sys
import time
import urllib.request

MODE = sys.argv[1]
M = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
G = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8002"
R = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8001"
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def gj(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def al_run_id():
    for _ in range(90):
        for a in gj(f"{M}/api/v1/agents")["items"]:
            if a["name"] == "al-run" and any(d["status"] == "ready" for d in gj(f"{M}/api/v1/deployments?agentId={a['id']}")["items"]):
                return a["id"]
        time.sleep(1)


def run(goal, tp=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"parts": [{"data": {"goal": goal}}]}}}
    h = {"content-type": "application/json", **({"traceparent": tp} if tp else {})}
    req = urllib.request.Request(f"{M}/api/v1/agents/{AID}/a2a", json.dumps(body).encode(), h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["result"]["task"]


def events(task_id):
    raw = urllib.request.urlopen(f"{R}/runs/{task_id}/events?follow=false", timeout=10).read().decode()
    out, kind = [], None
    for ln in raw.splitlines():
        if ln.startswith("event: "):
            kind = ln[7:]
        elif ln.startswith("data: "):
            out.append((kind, json.loads(ln[6:])))
    return out


lst = gj(f"{G}/v0.1/servers?search=demo")
ver = gj(f"{G}/v0.1/servers/io.prodai/demo/versions/1.0.0")
check("E7-03", (lst["metadata"]["count"], lst["servers"][0]["server"]["name"], lst["servers"][0]["_meta"]["io.prodai.atlas/registry"]["status"], len(ver["pins"])),
      (1, "io.prodai/demo", "approved", 4))
check("E7-04", (sorted(s["name"] for s in gj(f"{G}/v0.1/tenants/default/effective")["servers"]), gj(f"{G}/v0.1/tenants/shadow-team/effective")["servers"]),
      (["io.prodai/demo", "io.prodai/manager-api"], []))

AID = al_run_id()
trace, parent = secrets.token_hex(16), secrets.token_hex(8)
t = run('demo__search_docs({"query":"atlas"}) 로 문서를 찾아 요약', f"00-{trace}-{parent}-01")
res = t["artifacts"][0]["parts"][0]["data"]
ev = dict((k, v) for k, v in events(t["id"]) if k in ("mcp", "tools_selected"))
check("E7-05", (t["status"]["state"], "ProdAI Atlas" in res.get("output", ""), ev.get("mcp", {}).get("tools"), "demo__search_docs" in ev.get("tools_selected", {}).get("selected", [])),
      ("TASK_STATE_COMPLETED", True, 13, True))   # 데모 2 + Manager 읽기 GET 11 (M2: getAgentCard·searchA2ARegistry·listNotificationTemplates 추가)

t2 = run("manager_api__listCatalogUnits 로 카탈로그 단위를 조회")
check("E7-06", (t2["status"]["state"], "AL-PLAN" in t2["artifacts"][0]["parts"][0]["data"].get("output", "")), ("TASK_STATE_COMPLETED", True))

t3 = run('demo__delete_records({"table":"tmp"}) 로 정리')
check("E7-07", (t3["status"]["state"], "demo__delete_records" in t3["status"]["message"]["parts"][0]["text"]), ("TASK_STATE_INPUT_REQUIRED", True))


def spans(trace_id):
    out = subprocess.run(["docker", "run", "--rm", "-v", f"prodai-atlas-{MODE}_otelsink:/d:ro", "alpine:3.20", "sh", "-c", "cat /d/traces*.jsonl 2>/dev/null"],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        for rs in json.loads(line).get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                found += [sp for sp in ss.get("spans", []) if sp["traceId"] == trace_id]
    return found


mcp_span, run_span = {}, {}
for _ in range(20):
    time.sleep(1)
    sp = spans(trace)
    mcp_span = next((s for s in sp if s["name"] == "mcp.tools.call demo__search_docs"), {})
    run_span = next((s for s in sp if s["name"] == "invoke_agent al-run"), {})
    if mcp_span and run_span:
        break
attrs = {a["key"]: next(iter(a["value"].values())) for a in mcp_span.get("attributes", [])}
check("E7-10", (mcp_span.get("parentSpanId") == run_span.get("spanId") and bool(run_span), attrs.get("gen_ai.tool.name"), attrs.get("mcp.protocol.version")),
      (True, "demo__search_docs", "2026-07-28"))
sys.exit(failed)
