#!/usr/bin/env python3
"""Epic 5 E2E — 사용: e5_e2e.py <local|prod> [manager] [console]
E5-02 자동 배포 ready · E5-03 Manager 경유 A2A · E5-04 전 구간 trace · E5-05 배포 이벤트 · E5-08 콘솔 프록시."""
import base64
import json
import secrets
import subprocess
import sys
import time
import urllib.request

MODE = sys.argv[1]
M = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
C = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:5173"
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers, r.read().decode()


def gj(url):
    return json.loads(get(url)[2])


# E5-02
dep, agent = {}, {}
for _ in range(60):
    agents = [a for a in gj(f"{M}/api/v1/agents?unit=AL-PLAN")["items"] if a["name"] == "al-plan"]
    if agents:
        agent = agents[0]
        deps = [d for d in gj(f"{M}/api/v1/deployments?agentId={agent['id']}")["items"] if d["status"] != "stopped"]
        if deps and deps[0]["status"] == "ready":
            dep = gj(f"{M}/api/v1/deployments/{deps[0]['id']}")
            break
    time.sleep(1)
check("E5-02", (dep.get("status"), dep.get("env"), dep.get("driver"), (dep.get("card") or {}).get("name")), ("ready", "dev", "compose", "al-plan"))

# E5-03 · E5-04
trace, parent = secrets.token_hex(16), secrets.token_hex(8)
body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"parts": [{"text": "배포한다. 그리고 관측한다"}]}}}
req = urllib.request.Request(f"{M}/api/v1/agents/{agent.get('id')}/a2a", json.dumps(body).encode(),
                             {"content-type": "application/json", "traceparent": f"00-{trace}-{parent}-01"})
with urllib.request.urlopen(req, timeout=30) as r:
    hdr, task = r.headers, json.load(r)["result"]["task"]
plan = task["artifacts"][0]["parts"][0]["data"]
check("E5-03", (task["status"]["state"], plan["source"], hdr.get("x-atlas-deployment") == dep.get("id")), ("TASK_STATE_COMPLETED", "gateway:planner-default", True))


def spans_for(trace_id):
    out = subprocess.run(["docker", "run", "--rm", "-v", f"prodai-atlas-{MODE}_otelsink:/d:ro", "alpine:3.20", "sh", "-c", "cat /d/traces*.jsonl 2>/dev/null"],
                         capture_output=True, text=True, check=True).stdout
    found = {}
    for line in out.splitlines():
        for rs in json.loads(line).get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    if sp["traceId"] == trace_id:
                        found[sp["name"]] = sp
    return found


spans = {}
for _ in range(20):
    time.sleep(1)
    spans = spans_for(trace)
    if {"invoke_agent al-plan", "chat planner-default"} <= spans.keys():
        break
inv, chat = spans.get("invoke_agent al-plan", {}), spans.get("chat planner-default", {})
check("E5-04", (inv.get("parentSpanId") == parent, chat.get("parentSpanId") == inv.get("spanId") and bool(inv)), (True, True))

# E5-05
def last_subject(subj):
    p = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:latest",
                        "nats", "-s", "nats://nats:4222", "stream", "get", "PLT", "--last-for", subj, "-j"], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return json.loads(base64.b64decode(json.loads(p.stdout)["data"]))


started, succeeded = last_subject("plt.deploy.started"), last_subject("plt.deploy.succeeded")
check("E5-05", ((started or {}).get("specversion"), (succeeded or {}).get("data", {}).get("to")), ("1.0", "ready"))

# E5-08 콘솔 프록시
st, h, html = get(f"{C}/")
names = [a["name"] for a in gj(f"{C}/api/v1/agents")["items"]]
check("E5-08", (st, "<div id=\"root\">" in html, "al-plan" in names, gj(f"{C}/health/manager")["status"], gj(f"{C}/gw/health")["status"]),
      (200, True, True, "ok", "ok"))
sys.exit(failed)
