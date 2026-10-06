#!/usr/bin/env python3
"""Epic 3 AI Gateway 스모크 — local·prod 공용.
사용: e3_smoke.py <admin_token> <env:local|prod> [gateway_url] [agent_url] [nats_monitor]"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

ADMIN, ENV = sys.argv[1], sys.argv[2]
G = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8090"
A = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8000"
N = sys.argv[5] if len(sys.argv) > 5 else "http://127.0.0.1:8222"
failed = 0


def http(method, url, body=None, token=None, headers=None):
    h = {"content-type": "application/json", **(headers or {})}
    if token:
        h["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def js(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


def gw_msgs():
    d = js(http("GET", f"{N}/jsz?streams=true")[2])
    return sum(s["state"]["messages"] for a in d.get("account_details", []) for s in a.get("stream_detail", []) if s["name"] == "GW")


def chat(key, model, content="안녕", **extra):
    return http("POST", f"{G}/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": content}], **extra}, key)


ev0 = gw_msgs()
completed = 0
st, _, raw = http("GET", f"{G}/health")
check("E3-02", (st, js(raw).get("redis")), (200, "ok"))
st, _, raw = chat(None, "chat-default")
check("E3-03", (st, js(raw)["error"]["code"]), (401, "invalid_api_key"))

st, _, raw = http("POST", f"{G}/admin/keys", {"tenant": "smoke", "models": ["chat-*", "flaky-*"], "rpm": 3}, ADMIN)
issued = js(raw)
key, prefix = issued.get("key", ""), issued.get("virtualKey", {}).get("prefix", "")
st, h, raw = chat(key, "chat-default")
completed += st == 200
hl = {k.lower(): v for k, v in h.items()}
check("E3-04", (bool(re.fullmatch(rf"sk-atlas-{ENV}-[0-9A-Za-z]{{22}}", key)), st, bool(re.fullmatch(r"\d+\.\d{8}", hl.get("x-atlas-cost-usd", ""))), hl.get("x-atlas-route")),
      (True, 200, True, "mock/mock-chat"))
st, _, raw = chat(key, "planner-default")
check("E3-05", (st, js(raw)["error"]["code"]), (403, "model_not_allowed"))
# 403 은 정책 단계 거부라 RPM 을 쓰지 않음 → 허용 요청 3회(chat·flaky·chat) 후 4번째가 429
st, _, _ = chat(key, "flaky-chat")
completed += st == 200
st, _, _ = chat(key, "chat-default")
completed += st == 200
st, h, raw = chat(key, "chat-default")
hl = {k.lower(): v for k, v in h.items()}
check("E3-06", (st, js(raw)["error"]["code"], "retry-after" in hl), (429, "rate_limit_exceeded", True))

st, _, raw = http("POST", f"{G}/admin/keys", {"tenant": "smoke", "models": ["*"]}, ADMIN)  # 무제한 키
key2 = js(raw)["key"]
prefix2 = js(raw)["virtualKey"]["prefix"]
st, h, _ = chat(key2, "flaky-chat")
completed += st == 200
check("E3-07", (st, {k.lower(): v for k, v in h.items()}.get("x-atlas-fallback")), (200, "chat-default"))

st, h, raw = chat(key2, "chat-default", "스트리밍 확인", stream=True)
completed += st == 200
chunks = [line[6:] for line in raw.split("\n\n") if line.startswith("data: ")]
content = "".join(c["delta"].get("content", "") for x in chunks[:-1] for c in json.loads(x)["choices"])
usage = [json.loads(x).get("usage") for x in chunks[:-1] if json.loads(x).get("usage")]
check("E3-08", (chunks[-1], content, bool(usage)), ("[DONE]", "[mock:mock-chat] 스트리밍 확인", True))

u = js(http("GET", f"{G}/admin/usage?key_prefix={prefix2}", token=ADMIN)[2])
check("E3-09", (u.get("requests"), u.get("prompt_tokens", 0) > 0, float(u.get("cost_usd", "0")) > 0, u.get("by_alias")), (2, True, True, {"chat-default": 2}))

# E3-12: AL-PLAN → Gateway
task = js(http("POST", f"{A}/a2a", {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                                     "params": {"message": {"parts": [{"text": "설계한다. 그리고 구현한다. 그다음 배포한다"}]}}})[2])["result"]["task"]
completed += 1
plan = task["artifacts"][0]["parts"][0]["data"] if task["artifacts"] else {}
check("E3-12", (task["status"]["state"], plan.get("source"), len(plan.get("steps", [])), plan.get("dod", [{}])[0].get("type")),
      ("TASK_STATE_COMPLETED", "gateway:planner-default", 3, "test"))

time.sleep(1.5)
check("E3-10", gw_msgs() - ev0, completed)

st, _, _ = http("DELETE", f"{G}/admin/keys/{prefix2}", token=ADMIN)
st2, _, raw = chat(key2, "chat-default")
check("E3-13", (st, st2, js(raw)["error"]["code"]), (204, 401, "invalid_api_key"))
sys.exit(failed)
