#!/usr/bin/env python3
"""Epic 13 경제성(EC-*) 스택 스모크 — local·prod 공용. 실행마다 새 tenant·가상키를 발급해 서로 간섭하지 않는다.
사용: e13_econ.py <admin_token> [gateway_url] [network]
마지막에 레버별 비용 효과(E13-LEVERS)를 JSON 으로 출력한다 — M2 종료 게이트 '비용 레버 효과 측정' 근거."""
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal

ADMIN = sys.argv[1]
G = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8090"
NET = sys.argv[3] if len(sys.argv) > 3 else "prodai-atlas-local_default"
failed = 0
levers = {}


def http(method, url, body=None, token=None, headers=None, raw=None, ctype="application/json"):
    h = {"content-type": ctype, **(headers or {})}
    if token:
        h["authorization"] = f"Bearer {token}"
    data = raw.encode() if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()


def js(s):
    try:
        return json.loads(s)
    except ValueError:
        return {}


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


tenant = "econ-" + secrets.token_hex(3)
st, _, b = http("POST", f"{G}/admin/keys", {"tenant": tenant, "project": "p1", "models": ["*"], "rpm": 0, "tpm": 0}, ADMIN)
issued = js(b)
KEY, PREFIX = issued.get("key"), issued.get("virtualKey", {}).get("prefix")
print(f"tenant={tenant} prefix={PREFIX}")


def chat(model, user, extra=None, headers=None, system=None, key=None):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    return http("POST", f"{G}/v1/chat/completions", {"model": model, "messages": msgs, **(extra or {})}, key or KEY, headers)


def usd(h):
    return Decimal(h.get("x-atlas-cost-usd", "0"))


# E13-01 모델 카탈로그 (EC-MODEL-07)
st, _, b = http("GET", f"{G}/admin/econ/models", token=ADMIN)
cat = {m["model"]: m for m in js(b).get("models", [])}
check("E13-01", (st, cat.get("mock-reasoner", {}).get("reasoning"), bool(cat.get("mock-chat-large", {}).get("warning")),
                 "input" in (cat.get("mock-chat", {}).get("prices_per_1m") or {}), len(js(b).get("task_policies", [])) >= 5),
      (200, True, True, True, True))

# E13-02 규칙 라우팅 (EC-ROUTE-01) — opt-in alias chat-econ, pin 으로 우회
_, h1, _ = chat("chat-econ", "짧은 질문 하나")
_, h2, _ = chat("chat-econ", "짧은 질문 하나", headers={"x-atlas-route-pin": "true"})
_, h3, _ = chat("chat-econ", "긴 질문 " * 400)
check("E13-02", (h1.get("x-atlas-econ-route"), h1.get("x-atlas-route"), h2.get("x-atlas-route"), h3.get("x-atlas-route")),
      ("short-no-tools", "mock/mock-chat-small", "mock/mock-chat", "mock/mock-chat"))
levers["routing"] = {"routed_usd": str(usd(h1)), "pinned_usd": str(usd(h2)),
                     "saving_pct": float(round((1 - usd(h1) / usd(h2)) * 100, 1)) if usd(h2) else None}

# E13-03 과업 유형별 모델 정책·max_tokens 클램프 (EC-MODEL-01 · EC-CTRL-02) + auto
_, hc, bc = chat("chat-large", "이 문장의 감정을 분류", {"max_tokens": 500}, {"x-atlas-task-type": "classify"})
_, hl, _ = chat("chat-large", "이 문장의 감정을 분류", {"max_tokens": 500}, {"x-atlas-route-pin": "true"})
_, ha, _ = chat("auto", "어려운 증명", {"atlas": {"task_type": "reasoning"}})
check("E13-03", (hc.get("x-atlas-route"), "max_tokens:500->64" in hc.get("x-atlas-policy-mutations", ""), ha.get("x-atlas-route")),
      ("mock/mock-chat-small", True, "mock/mock-reasoner"))
levers["model_policy"] = {"policy_usd": str(usd(hc)), "large_usd": str(usd(hl)),
                          "saving_pct": float(round((1 - usd(hc) / usd(hl)) * 100, 1)) if usd(hl) else None}

# E13-04 reasoning effort 정규화 (EC-MODEL-02) — 과업 상한·비추론 모델 무시
_, he, be = chat("reasoner", "단계별로 풀어", {"atlas": {"effort": "high", "task_type": "code"}})
_, hn, _ = chat("chat-default", "단계별로 풀어", {"atlas": {"effort": "high"}})
check("E13-04", (he.get("x-atlas-effort"), js(be).get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens"),
                 "effort:ignored(non-reasoning)" in hn.get("x-atlas-policy-mutations", ""), "max_tokens:default=512" in hn.get("x-atlas-policy-mutations", "")),
      ("medium", 1024, True, True))

# E13-05 exact 응답 캐시 (EC-CACHE-04): 같은 요청 10회 → 1 MISS + 9 HIT, 비용 1/10
q = "exact-" + secrets.token_hex(4)
costs, states = [], []
for _ in range(10):
    _, h, _ = chat("chat-default", q, {"temperature": 0})
    costs.append(usd(h))
    states.append(h.get("x-atlas-cache"))
_, hb, _ = chat("chat-default", q, {"temperature": 0}, {"Cache-Control": "no-cache"})
_, ht, _ = chat("chat-default", q, {"temperature": 0.7})
check("E13-05", (states[0], states[1:].count("HIT-EXACT"), sum(costs[1:]), hb.get("x-atlas-cache"), ht.get("x-atlas-cache")),
      ("MISS", 9, Decimal("0E-8") * 9, "BYPASS", "SKIP-TEMPERATURE"))
levers["exact_cache"] = {"requests": 10, "with_cache_usd": str(sum(costs)), "without_cache_usd": str(costs[0] * 10),
                         "saving_pct": float(round((1 - sum(costs) / (costs[0] * 10)) * 100, 1)) if costs[0] else None}

# E13-06 공급사 prompt cache (EC-CACHE-01·03): 긴 고정 system + 바뀌는 질문 5회
sys_prompt = f"[{tenant}] 고정 시스템 지침·도구 설명·정책 문서. " * 300
paid, saved, reads = Decimal(0), Decimal(0), []
for i in range(5):
    _, h, b = chat("chat-default", f"질문 {i}", system=sys_prompt)
    paid += usd(h)
    saved += Decimal(h.get("x-atlas-cache-savings-usd", "0"))
    reads.append(int(h.get("x-atlas-cache-read-tokens", "0")))
    ttl = h.get("x-atlas-prompt-cache")
st, _, b = http("GET", f"{G}/admin/econ/cache?tenant={tenant}", token=ADMIN)
kpi = {k["alias"]: k for k in js(b).get("data", [])}.get("chat-default", {})
check("E13-06", (ttl, reads[0] == 0, all(r > 0 for r in reads[1:]), kpi.get("hit_ratio", 0) > 0.5, kpi.get("warning", "")),
      ("5m", True, True, True, ""))
levers["prompt_cache"] = {"requests": 5, "paid_usd": str(paid), "without_cache_usd": str(paid + saved),
                          "saving_pct": float(round(saved / (paid + saved) * 100, 1)) if paid + saved else None,
                          "hit_ratio": round(kpi.get("hit_ratio", 0), 3)}

# E13-07 계층 예산 (EC-CTRL-01): 경고(80%) → downgrade → block, 상태 이벤트
http("PUT", f"{G}/admin/budgets/{tenant}-key", {"scope": "key", "scope_id": PREFIX, "amount_usd": "0.00002", "period": "daily",
                                                  "hard_action": "downgrade", "downgrade_alias": "chat-small"}, ADMIN)
_, hw, _ = chat("chat-large", "예산 소모 " * 60, headers={"x-atlas-route-pin": "true"})
_, hd, _ = chat("chat-large", "하향 대상", headers={"x-atlas-route-pin": "true"})
http("PUT", f"{G}/admin/budgets/{tenant}-org", {"scope": "org", "scope_id": tenant, "amount_usd": "0.00001", "period": "monthly", "hard_action": "block"}, ADMIN)
sb, hbk, bb = chat("chat-default", "막혀야 함")
st, _, b = http("GET", f"{G}/admin/budgets", token=ADMIN)
states = {x["id"]: x["state"] for x in js(b).get("data", [])}
check("E13-07", (hd.get("x-atlas-route"), "budget_downgrade" in hd.get("x-atlas-policy-mutations", ""), sb, js(bb).get("error", {}).get("code"),
                 bool(hbk.get("x-atlas-budget-warning")), states.get(f"{tenant}-org")),
      ("mock/mock-chat-small", True, 429, "budget_exceeded", True, "exceeded"))
for i in ("key", "org"):
    http("DELETE", f"{G}/admin/budgets/{tenant}-{i}", token=ADMIN)

# E13-08 과업 토큰 예산 (EC-CTRL-03): x-atlas-task-id 누적 → 80% 경고 → 100% 429
task = "task-" + secrets.token_hex(4)
hdr = {"x-atlas-task-id": task, "x-atlas-task-budget": "300"}
s1, t1, _ = chat("chat-default", "작업 " * 100, headers=hdr)
s2, t2, _ = chat("chat-default", "작업 " * 100, headers=hdr)
s3, t3, b3 = chat("chat-default", "작업", headers=hdr)
used = lambda h: int(h.get("x-atlas-task-tokens", "0/0").split("/")[0])
check("E13-08", (s1, used(t2) > used(t1) > 0, s3, js(b3).get("error", {}).get("code"), t1.get("x-atlas-task-tokens", "").endswith("/300")),
      (200, True, 429, "task_budget_exceeded", True))

# E13-09 배치 (EC-BATCH-01·02): JSONL 제출 → 폴링 → ended → 결과·실패 행 재제출, 50% 할인
lines = [{"custom_id": f"r{i}", "method": "POST", "url": "/v1/chat/completions",
          "body": {"model": "chat-default", "messages": [{"role": "user", "content": f"배치 요약 {i}" + (" batch-fail" if i == 2 else "")}]}} for i in range(3)]
st, _, b = http("POST", f"{G}/v1/batches", token=KEY, raw="\n".join(json.dumps(x, ensure_ascii=False) for x in lines), ctype="application/jsonl")
bid = js(b).get("id")
batch = {}
for _ in range(60):
    batch = js(http("GET", f"{G}/v1/batches/{bid}", token=KEY)[2])
    if batch.get("status") not in ("validating", "in_progress"):
        break
    time.sleep(1)
out = http("GET", f"{G}/v1/batches/{bid}/output", token=KEY)[2]
rows = [json.loads(x) for x in out.splitlines() if x.strip()]
rs, _, rb = http("POST", f"{G}/v1/batches/{bid}/retry", token=KEY)
cross = http("GET", f"{G}/v1/batches/{bid}", token=None)[0]
check("E13-09", (st, batch.get("status"), batch.get("completed"), batch.get("failed"), sorted(r["custom_id"] for r in rows),
                 rs, js(rb).get("round"), cross),
      (200, "ended", 2, 1, ["r0", "r1", "r2"], 200, 1, 401))
cost_b, sav_b = Decimal(batch.get("cost_usd", "0")), Decimal(batch.get("savings_usd", "0"))
levers["batch"] = {"rows_ok": batch.get("completed"), "paid_usd": str(cost_b), "without_batch_usd": str(cost_b + sav_b),
                   "saving_pct": float(round(sav_b / (cost_b + sav_b) * 100, 1)) if cost_b + sav_b else None}

# E13-10 시뮬레이터 (EC-CTRL-06)
st, _, b = http("POST", f"{G}/v1/econ/simulate", {"model": "mock-chat-large", "cache_ratio": 0.6, "batch_ratio": 0.3, "route_ratio": 0.4,
                                                   "route_model": "mock-chat-small", "alt_model": "mock-chat", "tasks_per_day": 1000})
sim = js(b)
steps = sim.get("waterfall", [])
check("E13-10", (st, [s["lever"] for s in steps], all(steps[i]["monthly_usd"] < steps[i - 1]["monthly_usd"] for i in range(1, len(steps)))),
      (200, ["baseline", "cache", "batch", "routing", "model"], True))
levers["simulator"] = {"baseline_monthly_usd": steps[0]["monthly_usd"] if steps else None, "final_monthly_usd": sim.get("final_monthly_usd"),
                       "total_saving_pct": sim.get("total_saving_pct")}

# E13-11 이벤트 (NATS GW 스트림 subject) — budget.state_changed · task_near_limit · batch.completed
try:
    subj = subprocess.run(["docker", "run", "--rm", "--network", NET, "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222",
                           "stream", "subjects", "GW", "--json"], capture_output=True, text=True, timeout=90).stdout
    seen = js(subj) or {}
except (subprocess.SubprocessError, FileNotFoundError):
    seen = {}
check("E13-11", tuple(s in seen for s in ("gw.budget.state_changed", "gw.budget.task_near_limit", "gw.batch.completed")), (True, True, True))

print("E13-LEVERS " + json.dumps(levers, ensure_ascii=False))
http("DELETE", f"{G}/admin/keys/{PREFIX}", token=ADMIN)
sys.exit(1 if failed else 0)
