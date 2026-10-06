#!/usr/bin/env python3
"""Epic 4 OB-TRACE 스모크 — 사용: e4_trace.py <local|prod> [agent_url] [gateway_url]
A2A 요청에 고정 traceparent 를 넣고, Collector file 싱크에서 span 트리를 확인한다."""
import json
import secrets
import subprocess
import sys
import time
import urllib.request

MODE = sys.argv[1]
A = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
G = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8090"
TRACE, PARENT = secrets.token_hex(16), secrets.token_hex(8)
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def spans_for(trace_id):
    out = subprocess.run(["docker", "run", "--rm", "-v", f"prodai-atlas-{MODE}_otelsink:/d:ro", "alpine:3.20", "sh", "-c", "cat /d/traces*.jsonl 2>/dev/null"],
                         capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        for rs in json.loads(line).get("resourceSpans", []):
            res = {a["key"]: next(iter(a["value"].values())) for a in rs["resource"].get("attributes", [])}
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    if sp["traceId"] == trace_id:
                        sp["attrs"] = {a["key"]: next(iter(a["value"].values()), None) for a in sp.get("attributes", [])}
                        sp["service"] = res.get("service.name")
                        found.append(sp)
    return found


body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"parts": [{"text": "관측한다. 그리고 검증한다"}]}}}
req = urllib.request.Request(f"{A}/a2a", json.dumps(body).encode(), {"content-type": "application/json", "traceparent": f"00-{TRACE}-{PARENT}-01"})
task = json.load(urllib.request.urlopen(req, timeout=30))["result"]["task"]
print(f"trace_id={TRACE} task={task['id']} state={task['status']['state']}")

spans = []
for _ in range(20):
    time.sleep(1)
    spans = spans_for(TRACE)
    if {s["name"] for s in spans} >= {"invoke_agent al-plan", "chat planner-default"}:
        break
by = {s["name"]: s for s in spans}
agent, chat = by.get("invoke_agent al-plan", {}), by.get("chat planner-default", {})
check("E4-02", len(spans) >= 2, True)
check("E4-03", (agent.get("service"), agent.get("parentSpanId"), agent.get("attrs", {}).get("gen_ai.operation.name")),
      ("atlas-agent-al-plan", PARENT, "invoke_agent"))
check("E4-04", (chat.get("service"), chat.get("parentSpanId") == agent.get("spanId")), ("atlas-gateway", True))
a = chat.get("attrs", {})
check("E4-05", (a.get("gen_ai.operation.name"), a.get("gen_ai.provider.name"), a.get("gen_ai.request.model"), a.get("gen_ai.response.model"),
                int(a.get("gen_ai.usage.input_tokens", 0)) > 0, int(a.get("gen_ai.usage.output_tokens", 0)) > 0,
                bool(a.get("atlas.cost.usd")), a.get("atlas.unit_id")),
      ("chat", "mock", "planner-default", "mock-planner", True, True, True, "AL-PLAN"))

# E4-06: 직접 호출 시 응답 헤더 trace id
TRACE2 = secrets.token_hex(16)
from urllib.error import HTTPError  # noqa: E402
key = sys.argv[4] if len(sys.argv) > 4 else None
if key:
    r = urllib.request.Request(f"{G}/v1/chat/completions", json.dumps({"model": "chat-default", "messages": [{"role": "user", "content": "t"}]}).encode(),
                               {"content-type": "application/json", "authorization": f"Bearer {key}", "traceparent": f"00-{TRACE2}-{secrets.token_hex(8)}-01"})
    with urllib.request.urlopen(r, timeout=10) as res:
        check("E4-06", res.headers.get("x-atlas-trace-id"), TRACE2)
sys.exit(failed)
