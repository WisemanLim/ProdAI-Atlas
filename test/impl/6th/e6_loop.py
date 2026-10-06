#!/usr/bin/env python3
"""Epic 6 루프 E2E — 사용: e6_loop.py <local|prod> [manager] [al_run_url]"""
import json
import secrets
import subprocess
import sys
import time
import urllib.request

MODE = sys.argv[1]
M = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
R = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8001"
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def gj(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def agent_id(name):
    for _ in range(90):
        for a in gj(f"{M}/api/v1/agents")["items"]:
            if a["name"] == name:
                deps = [d for d in gj(f"{M}/api/v1/deployments?agentId={a['id']}")["items"] if d["status"] == "ready"]
                if deps:
                    return a["id"], deps[0]
        time.sleep(1)
    return None, {}


def proxy(aid, data, task_id=None, traceparent=None):
    msg = {"role": "ROLE_USER", "parts": [{"data": data}], **({"taskId": task_id} if task_id else {})}
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}}
    h = {"content-type": "application/json", **({"traceparent": traceparent} if traceparent else {})}
    req = urllib.request.Request(f"{M}/api/v1/agents/{aid}/a2a", json.dumps(body).encode(), h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["result"]["task"]


def result(task):
    return task["artifacts"][0]["parts"][0]["data"] if task["artifacts"] else {}


aid, dep = agent_id("al-run")
card = gj(f"{M}/api/v1/deployments/{dep.get('id')}").get("card", {})
check("E6-02", (bool(aid), [s["id"] for s in card.get("skills", [])]), (True, ["loop.run", "loop.resume"]))

trace, parent = secrets.token_hex(16), secrets.token_hex(8)
t = proxy(aid, {"goal": 'atlas_calc({"expr":"6*7"}) 로 계산해 답한다', "dod": [{"type": "test", "spec": "contains:42"}]}, traceparent=f"00-{trace}-{parent}-01")
r = result(t)
events = gj(f"{R}/runs/{t['id']}") if False else None
sse = urllib.request.urlopen(f"{R}/runs/{t['id']}/events?follow=false", timeout=10).read().decode()
kinds = [ln.split(": ", 1)[1] for ln in sse.splitlines() if ln.startswith("event: ")]
plan_ev = [json.loads(ln[6:]) for ln in sse.splitlines() if ln.startswith("data: ")][0]
# impl 8th 갱신: Epic 7·8 이후 도구 결과는 spotlight 로 감싸지고(mock 은 그대로 되풀이) mcp·guard 이벤트가 끼므로 핵심 불변식만 본다
check("E6-03", (t["status"]["state"], r.get("reason"), "42" in r.get("output", ""), plan_ev.get("source")),
      ("TASK_STATE_COMPLETED", "GOAL_VERIFIED", True, "gateway:planner-default"))
core = [k for k in kinds if k in ("plan", "tool_call", "observation", "reflect", "exit")]
check("E6-08", core, ["plan", "tool_call", "observation", "reflect", "reflect", "exit"])

h1 = proxy(aid, {"goal": "atlas_delete_all 로 저장소를 정리한다"})
h2 = proxy(aid, {"approval": "approve"}, task_id=h1["id"])
check("E6-04", (h1["status"]["state"], h2["status"]["state"], result(h2).get("reason")), ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED", "END_TURN"))

lp = proxy(aid, {"goal": "repeat-forever atlas_echo"})
lsse = urllib.request.urlopen(f"{R}/runs/{lp['id']}/events?follow=false", timeout=10).read().decode()
check("E6-05", (result(lp).get("reason"), "event: replan" in lsse), ("LOOP_DETECTED", True))

mt = proxy(aid, {"goal": "never-finish atlas_echo", "budget": {"max_turns": 3}})
check("E6-06", (result(mt).get("reason"), result(mt).get("turns"), result(mt).get("status")), ("MAX_TURNS", 3, "truncated"))


def spans(trace_id):
    out = subprocess.run(["docker", "run", "--rm", "-v", f"prodai-atlas-{MODE}_otelsink:/d:ro", "alpine:3.20", "sh", "-c", "cat /d/traces*.jsonl 2>/dev/null"],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        for rs in json.loads(line).get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                found += [sp for sp in ss.get("spans", []) if sp["traceId"] == trace_id]
    return found


sp = []
for _ in range(20):
    time.sleep(1)
    sp = spans(trace)
    names = [s["name"] for s in sp]
    if {"invoke_agent al-run", "invoke_agent al-plan"} <= set(names) and names.count("chat agent-default") >= 2:
        break
by_id = {s["spanId"]: s for s in sp}
run_span = next((s for s in sp if s["name"] == "invoke_agent al-run"), {})
plan_span = next((s for s in sp if s["name"] == "invoke_agent al-plan"), {})
chats = [s for s in sp if s["name"] == "chat agent-default"]
check("E6-07", (run_span.get("parentSpanId") == parent, plan_span.get("parentSpanId") == run_span.get("spanId"),
                len(chats) >= 2 and all(c.get("parentSpanId") == run_span.get("spanId") for c in chats)), (True, True, True))
sys.exit(failed)
