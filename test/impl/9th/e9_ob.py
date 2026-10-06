#!/usr/bin/env python3
"""Epic 9 Observability E2E — 사용: e9_ob.py <local|prod> <bootstrap_key> <ch_password>"""
import base64
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MODE, KEY, CHPW = sys.argv[1:4]
M, G, OB, CH = "http://127.0.0.1:8080", "http://127.0.0.1:8090", "http://127.0.0.1:8006", "http://127.0.0.1:8123"
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, {"content-type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}, json.loads(e.read() or b"{}")


def chq(sql):
    req = urllib.request.Request(f"{CH}/?default_format=JSON", sql.encode(), {"Authorization": "Basic " + base64.b64encode(f"atlas:{CHPW}".encode()).decode()})
    return json.load(urllib.request.urlopen(req, timeout=30))["data"]


def agent_id(name):
    for _ in range(90):
        for a in http("GET", f"{M}/api/v1/agents")[2]["items"]:
            if a["name"] == name and any(d["status"] == "ready" for d in http("GET", f"{M}/api/v1/deployments?agentId={a['id']}")[2]["items"]):
                return a["id"]
        time.sleep(1)


def wait_trace(t, need=3, service=None):
    # impl 15th 갱신: 에이전트·가드 span 이 먼저 들어와 개수만 채울 수 있다 → 필요하면 특정 서비스 span 까지 기다린다
    for _ in range(45):
        n = chq(f"SELECT count() AS n FROM otel.otel_traces WHERE TraceId = '{t}'")[0]["n"]
        s = chq(f"SELECT count() AS n FROM otel.otel_traces WHERE TraceId = '{t}' AND ServiceName = '{service}'")[0]["n"] if service else 1
        if int(n) >= need and int(s) >= 1:
            return int(n)
        time.sleep(1)
    return 0


AL_RUN = agent_id("al-run")
trace, parent = secrets.token_hex(16), secrets.token_hex(8)
body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"parts": [{"data": {"goal": 'atlas_calc({"expr":"6*7"}) 로 계산', "dod": [{"type": "test", "spec": "contains:42"}]}}]}}}
task = http("POST", f"{M}/api/v1/agents/{AL_RUN}/a2a", body, {"traceparent": f"00-{trace}-{parent}-01"})[2]["result"]["task"]
n = wait_trace(trace, 6, "atlas-gateway")

ttl = chq("SELECT engine_full FROM system.tables WHERE database = 'otel' AND name = 'otel_traces'")[0]["engine_full"]
probes = chq("SELECT count() AS n FROM otel.otel_traces WHERE SpanName IN ('GET /readyz', 'GET /healthz', 'GET /health') AND Timestamp > now() - INTERVAL 2 MINUTE")[0]["n"]
check("E9-02", (n >= 6, "TTL" in ttl and ("toIntervalDay(30)" in ttl or "toIntervalHour(720)" in ttl or "2592000" in ttl), int(probes)), (True, True, 0))

q = f'gen_ai.request.model = "agent-default" AND trace_id = "{trace}"'
items = http("GET", f"{OB}/v1/traces?" + urllib.parse.urlencode({"q": q, "since": 3600}))[2]["items"]
it = items[0] if items else {}
check("E9-03", (len(items), "agent-default" in it.get("models", []), float(it.get("cost_usd", 0)) > 0, {"atlas-gateway", "atlas-agent-al-run", "atlas-agent-al-plan"} <= set(it.get("services", []))),
      (1, True, True, True))

tree = http("GET", f"{OB}/v1/traces/{trace}")[2]
edges = {(e["from"], e["to"]) for e in tree.get("graph", [])}
check("E9-04", (tree.get("spans", 0) >= 6, ("atlas-agent-al-run", "atlas-gateway") in edges, ("atlas-agent-al-run", "atlas-agent-al-plan") in edges, tree["waterfall"][0]["offset_ms"] == 0.0),
      (True, True, True, True))

sess = http("GET", f"{OB}/v1/sessions/{task['contextId']}")[2]["traces"]
check("E9-05", trace in [s["trace_id"] for s in sess], True)

st, h, _ = http("POST", f"{G}/v1/chat/completions", {"model": "chat-default", "messages": [{"role": "user", "content": "연락처 010-1234-5678 키 AKIAABCDEFGHIJKLMNOP 확인"}]}, {"authorization": f"Bearer {KEY}"})
t2 = h.get("x-atlas-trace-id")
wait_trace(t2, 1, "atlas-gateway")
attrs = chq(f"SELECT SpanAttributes['gen_ai.input.messages'] AS i, Events.Name AS ev FROM otel.otel_traces WHERE TraceId = '{t2}' AND ServiceName = 'atlas-gateway'")[0]
nats = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:latest", "nats", "-s", "nats://nats:4222",
                       "stream", "get", "GW", "--last-for", "gw.secret_leak_detected", "-j"], capture_output=True, text=True)
leak = json.loads(base64.b64decode(json.loads(nats.stdout)["data"]))["data"] if nats.returncode == 0 else {}
# impl 27th 이후: GR-PII-08 이 입력 경계에서 비밀을 먼저 <SECRET_*> 로 가림 → Gateway span 에는 원문이 닿지 않는다(Collector [SECRET] 정화는 2차 방어)
guard_first = "SECRET_AWS_ACCESS_KEY" in attrs["i"]   # span JSON 은 < > 를 \u003c \u003e 로 이스케이프
masked = "[SECRET]" in attrs["i"] or guard_first
# 가드가 먼저 가렸으면 Gateway 에 원문이 없어 gw.secret_leak_detected 도 없다 — 그 경우 건수는 검사하지 않는다(이전 PASS 는 스트림의 옛 메시지)
check("E9-06", ("010-1234-5678" in attrs["i"], "***-****-5678" in attrs["i"], "AKIA" in attrs["i"], masked, 2 if guard_first else leak.get("count")),
      (False, True, False, True, 2))   # mock 이 입력을 되풀이 → 입력·출력 본문에서 각 1건

evs = chq(f"SELECT groupUniqArrayArray(Events.Name) AS e FROM otel.otel_traces WHERE TraceId IN ('{trace}', '{t2}')")[0]["e"]
# 가드가 먼저 가렸으면(guard_first) span 정화 이벤트는 생기지 않는다(정상)
check("E9-07", ({"atlas.guardrail.decision", "atlas.loop.exit"} <= set(evs) and (guard_first or "atlas.ob.secret_leak_detected" in evs)), True)

st, _, rp = http("POST", f"{OB}/v1/replay", {"trace_id": trace, "model": "chat-default"})
check("E9-08", (st, rp.get("original", {}).get("model"), rp.get("replay", {}).get("model"), bool(rp.get("replay", {}).get("output"))),
      (200, "planner-default", "chat-default", True))   # span_id 생략 시 trace 의 첫 chat span = AL-PLAN 계획 호출

dash = http("GET", f"{OB}/v1/dashboards/llm?group_by=model&since=3600")[2]
models = {r["grp"]: r for r in dash["summary"]}
costs = {r["grp"]: r for r in http("GET", f"{OB}/v1/costs?group_by=agent&since=3600")[2]["items"]}
check("E9-09", ("agent-default" in models and int(models["agent-default"]["requests"]) >= 2 and float(models["agent-default"]["p95_ms"]) > 0,
                float(costs.get("AL-RUN", {}).get("cost_usd", 0)) > 0 and float(costs.get("AL-PLAN", {}).get("cost_usd", 0)) > 0), (True, True))

slo = http("POST", f"{OB}/v1/slos", {"name": f"chat p95 (e9 {secrets.token_hex(2)})", "kind": "latency", "objective": 0.99, "threshold_ms": 0.0001})[2]
status = http("GET", f"{OB}/v1/slos/{slo['id']}/status")[2]
hook = f"http://agent-ob:8000/v1/alerts/echo" if MODE == "local" else None
rule = http("POST", f"{OB}/v1/alerts/rules", {"name": f"e9 burn {secrets.token_hex(2)}", "severity": "critical", "webhook_url": hook,
                                             "expr": {"type": "slo_burn", "slo_id": slo["id"], "short": 1, "long": 1}})[2]
tr = [t for t in http("POST", f"{OB}/v1/alerts/evaluate")[2]["transitions"] if t["rule"] == rule["name"]]
again = [t for t in http("POST", f"{OB}/v1/alerts/evaluate")[2]["transitions"] if t["rule"] == rule["name"]]
echo_ok = True
if MODE == "local":
    echo = http("GET", f"{OB}/v1/alerts/echo")[2]["items"]
    echo_ok = any(e["rule"] == rule["name"] and e["traces"] for e in echo)
check("E9-10", (status["windows"]["1h"]["burn_rate"] > 1, len(tr), tr[0]["status"] if tr else None, tr[0]["delivered"] if tr else None, again, echo_ok),
      (True, 1, "firing", "sent" if MODE == "local" else "none", [], True))
sys.exit(failed)
