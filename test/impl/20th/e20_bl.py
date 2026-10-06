"""Epic 20 Bitter Lesson 스택 시험 — 실제 Gateway(alias 오버레이·계산 정책)·EV·AL-PLAN·AL-RUN·flagd·BL 로 모델 교체·하네스·계산을 검증.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/20th/e20_bl.py <local|prod>
env: ATLAS_GATEWAY_ADMIN_TOKEN · ATLAS_GATEWAY_AL_PLAN_KEY · ATLAS_EV_CURATOR_TOKEN · ATLAS_TEST_TOKEN(prod, Manager)"""
import json
import os
import secrets
import subprocess
import sys
import time
import uuid

import httpx

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
BL, EV, G = "http://127.0.0.1:8015", "http://127.0.0.1:8007", "http://127.0.0.1:8090"
M = os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")   # 호스트 8080 이 다른 프로젝트에 점유된 경우 MANAGER_PORT 와 함께
ADMIN, KEY, CUR = os.environ["ATLAS_GATEWAY_ADMIN_TOKEN"], os.environ["ATLAS_GATEWAY_AL_PLAN_KEY"], os.environ["ATLAS_EV_CURATOR_TOKEN"]
MH = {"Authorization": f"Bearer {os.environ['ATLAS_TEST_TOKEN']}"} if os.environ.get("ATLAS_TEST_TOKEN") else {}
NET = f"prodai-atlas-{MODE}_default"
N = secrets.token_hex(3)
c = httpx.Client(timeout=300)
GA = {"Authorization": f"Bearer {ADMIN}"}
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def j(r):
    if r.status_code >= 400:
        print("  !", r.status_code, r.text[:300], flush=True)
    return r.json() if r.content else {}


def chat(alias, key=None, **kw):
    h = {"Authorization": f"Bearer {KEY}", **({"x-atlas-assign-key": key} if key else {}), **kw.pop("headers", {})}
    r = c.post(f"{G}/v1/chat/completions", json={"model": alias, "messages": [{"role": "user", "content": kw.pop("text", f"질문 {N}")}], **kw}, headers=h)
    return r.status_code, r.headers.get("x-atlas-route", ""), r.headers


def dataset(name, inputs):
    c.post(f"{EV}/v1/datasets", json={"name": name})
    for inp in inputs:
        it = c.post(f"{EV}/v1/datasets/{name}/items", json={"input": inp}).json()
        c.post(f"{EV}/v1/items/{it['id']}/transition", json={"to": "in_review"})
        c.post(f"{EV}/v1/items/{it['id']}/transition", json={"to": "approved", "verdict": "pass"}, headers={"authorization": f"Bearer {CUR}"})
    c.post(f"{EV}/v1/datasets/{name}/versions")


def al_plan_id():
    for a in c.get(f"{M}/api/v1/a2a/registry/agents", params={"limit": 50}, headers=MH).json()["items"]:
        if a["name"] == "al-plan":
            return a["agentId"]


def plan(goal, model_alias=None):
    data = {"goal": goal, **({"model_alias": model_alias} if model_alias else {})}
    r = c.post(f"{M}/api/v1/agents/{PLAN}/a2a", headers=MH, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
        "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": data}]}}}).json()
    return r["result"]["task"]


PLAN = al_plan_id()
ALIAS = f"e20-chat-{N}"

# 격리용 alias (다른 시험의 chat-default 를 흔들지 않는다)
j(c.put(f"{G}/admin/aliases/{ALIAS}", headers=GA, json={"deployments": [{"provider": "mock", "model": "mock-chat", "weight": 1}], "note": "e20"}))
dataset(f"bl-qa-{N}", [{"q": f"질문 {i} {N}"} for i in range(6)])
SUITE = {"name": f"bl-{N}", "dataset": {"name": f"bl-qa-{N}"}, "scorers": [{"type": "contains", "value": "[mock:"}],
         "targets": [{"id": "prod", "type": "prompt", "model": ALIAS, "template": "{{input.q}}"}]}

# E20-01 카탈로그 · 후보 0% 등록 → 기준·후보 회귀(pass^4) → 채택 규칙 (MODEL-01·02·03·04)
cat = {m["model"]: m for m in j(c.get(f"{BL}/v1/models"))["items"]}
cand = j(c.post(f"{BL}/v1/models/mock-chat-small/candidate", json={"alias": ALIAS, "suites": [SUITE], "k": 4}))
al = j(c.get(f"{G}/admin/aliases/{ALIAS}", headers=GA))
m = cand.get("detail", {}).get("metrics", {})
temp_left = [a["name"] for a in j(c.get(f"{G}/admin/aliases", headers=GA))["data"] if a["name"].startswith("bl-")]
check("E20-01", ("mock-chat-small" in cat, cand.get("status"), cand.get("decision"), m.get("cand_cost_per_case", 1) < m.get("base_cost_per_case", 0),
                 {d["model"]: d["weight"] for d in al["deployments"]}, temp_left),
      (True, "decided", "promote", True, {"mock-chat": 1, "mock-chat-small": 0}, []))

# E20-02 카나리 20% · 고정 배정 · 통계 → 승격(기존 모델 폴백) (MODEL-05·06)
cid = cand["id"]
st = j(c.post(f"{BL}/v1/candidates/{cid}/canary", json={"stages": [20, 100], "min_samples": 5}))
routes, sticky = {}, True
for i in range(60):
    _, r1, _ = chat(ALIAS, key=f"user-{i}")
    _, r2, _ = chat(ALIAS, key=f"user-{i}")
    sticky &= r1 == r2
    routes[r1] = routes.get(r1, 0) + 1
adv = j(c.post(f"{BL}/v1/candidates/{cid}/canary/advance"))
al2 = j(c.get(f"{G}/admin/aliases/{ALIAS}", headers=GA))
_, served, _ = chat(ALIAS)
check("E20-02", (sticky, set(routes) == {"mock/mock-chat", "mock/mock-chat-small"}, adv.get("action"), [d["model"] for d in al2["deployments"]],
                 al2["fallbacks"], served),
      (True, True, "promoted", ["mock-chat-small"], [f"{ALIAS}-prev"], "mock/mock-chat-small"))

# E20-03 원클릭 롤백 → 등록 전 alias 버전 · 감사·이벤트 (MODEL-06)
rb = j(c.post(f"{BL}/v1/candidates/{cid}/rollback", json={"by": "e20", "reason": "시험 복원"}))
al3 = j(c.get(f"{G}/admin/aliases/{ALIAS}", headers=GA))
aud = [a["action"] for a in j(c.get(f"{BL}/v1/audit", params={"target": f"alias:{ALIAS}"}))["items"]]
check("E20-03", (rb.get("status"), [(d["model"], d["weight"]) for d in al3["deployments"]], {"candidate.register", "candidate.decide", "candidate.promote", "candidate.rollback"} <= set(aud)),
      ("rolled_back", [("mock-chat", 1)], True))

# E20-04 비싼 후보 → hold · force 카나리 → 비용 SLO 위반 자동 롤백 (MODEL-04·05)
big = j(c.post(f"{BL}/v1/models/mock-chat-large/candidate", json={"alias": ALIAS, "suites": [SUITE], "k": 2}))
nf = c.post(f"{BL}/v1/candidates/{big['id']}/canary", json={}).status_code
j(c.post(f"{BL}/v1/candidates/{big['id']}/canary", json={"stages": [50, 100], "min_samples": 5, "force": True}))
for i in range(40):
    chat(ALIAS, key=f"u{i}")
slo = j(c.post(f"{BL}/v1/candidates/{big['id']}/canary/advance"))
al4 = j(c.get(f"{G}/admin/aliases/{ALIAS}", headers=GA))
check("E20-04", (big.get("decision"), nf, slo.get("action"), any("비용" in x for x in slo.get("reasons", [])), [d["model"] for d in al4["deployments"] if d["weight"] > 0]),
      ("hold", 422, "rolled_back", True, ["mock-chat"]))

# E20-05 수명종료 스캔 → 대체 후보 등록(0%) · 모델 카드 (MODEL-07·08)
eol = j(c.get(f"{BL}/v1/models/eol"))["items"]
large = next((e for e in eol if e["model"] == "mock-chat-large"), {})
scan = j(c.post(f"{BL}/v1/models/eol/scan", json={}))
reg = [r for r in scan.get("registered", []) if r["alias"] == "chat-large"]
for r in scan.get("registered", []):
    c.post(f"{BL}/v1/candidates/{r['id']}/rollback", json={"by": "e20", "reason": "시험 정리"})
card = c.get(f"{BL}/v1/models/mock-chat-small/card").text
check("E20-05", ("chat-large" in large.get("aliases", []), large.get("replacement"), len(reg) <= 1, "# 모델 카드 — mock-chat-small" in card and "candidate.promote" in card),
      (True, "mock-agent", True, True))   # 같은 공급자·도구·컨텍스트 조건 중 최저가

# E20-06 계산 정책: auto 난이도 · 추론 토큰 예산 80% → 하향 (COMP-01·02·05)
unit = f"E20-{N}"
j(c.put(f"{G}/admin/compute-policies/e20-{N}", headers=GA, json={"match": {"agent": unit}, "effort": "auto", "reasoning_budget_tokens": 3000}))
tags = {"x-atlas-tags": f"unit_id:{unit}"}
_, _, he = chat("reasoner", text=f"영어로 번역해줘 {N}", headers=tags)
_, _, hh = chat("reasoner", text=f"이 알고리즘이 왜 빠른지 단계별로 증명해줘 {N}", headers=tags)
down = None
for i in range(10):
    _, _, h = chat("reasoner", text=f"단계별로 증명해줘 왜 알고리즘이 {i} {N}", headers=tags)
    if h.get("x-atlas-compute-downgraded"):
        down = h["x-atlas-compute-downgraded"]
        break
use = j(c.get(f"{G}/admin/compute-policies/e20-{N}/usage", headers=GA))
c.delete(f"{G}/admin/compute-policies/e20-{N}", headers=GA)
check("E20-06", (he.get("x-atlas-difficulty"), he.get("x-atlas-effort"), hh.get("x-atlas-difficulty"), hh.get("x-atlas-effort"), int(hh.get("x-atlas-reasoning-tokens", "0")) > 0,
                 down, use.get("pct", 0) >= 80),
      ("easy", "low", "hard", "high", True, "high->medium", True))

# E20-07 프롬프트 레지스트리 · 게시 게이트 (HARN-01·06) — v2 는 "Reply in prose" 로 계획 JSON 을 깨뜨림
c.put(f"{BL}/v1/flags/harness.plan_heuristic_fallback.enabled", json={"enabled": False, "reason": "게이트 시험 중 폴백 끔", "introduced_for": "mock-planner",
                                                                   "removal_condition": "ablation removable"})
dataset(f"goals-{N}", [{"goal": f"자료를 모으고 정리한 뒤 보고서를 쓴다 {i} {N}"} for i in range(3)])
GATE = {"name": f"plan-gate-{N}", "dataset": {"name": f"goals-{N}"}, "scorers": [{"type": "contains", "value": "steps"}], "thresholds": {"pass_rate": 1.0},
        "targets": [{"id": "al-plan", "type": "agent", "url": "http://agent-al-plan:8000", "skill": "loop.plan", "data_template": {"goal": "{{input.goal}}"}}]}
p1 = j(c.post(f"{BL}/v1/prompts/al-plan.system", json={"template": "You are a planner. Decompose the goal into ordered steps and a definition of done. Reply with JSON only."}))
p2 = j(c.post(f"{BL}/v1/prompts/al-plan.system", json={"template": "You are a planner. Reply in prose, no JSON."}))
g1 = c.post(f"{BL}/v1/prompts/al-plan.system/labels/production", json={"version": p1["version"], "gate_suite": GATE})
g2 = c.post(f"{BL}/v1/prompts/al-plan.system/labels/production", json={"version": p2["version"], "gate_suite": GATE})
prod = j(c.get(f"{BL}/v1/prompts/al-plan.system", params={"label": "production"}))
check("E20-07", (g1.status_code, g1.json().get("gate", {}).get("gate"), g2.status_code, prod.get("version") == p1["version"]), (200, "pass", 409, True))

# E20-08 보정 플래그 (flagd OFREP) — 약한 플래너의 군말 JSON 복구를 켜고 끔 (HARN-04)
def set_flag(k, v):
    return c.put(f"{BL}/v1/flags/{k}", json={"enabled": v, "reason": "약한 플래너 JSON 군말 보정", "introduced_for": "mock-planner-sloppy",
                                            "removal_condition": "ablation removable 판정"}).status_code


s1 = set_flag("harness.plan_json_repair.enabled", True)
time.sleep(7)
on = plan(f"자료를 모은다 그리고 정리한다 {N}", "planner-sloppy")
set_flag("harness.plan_json_repair.enabled", False)
time.sleep(7)
off = plan(f"자료를 모은다 그리고 정리한다 2 {N}", "planner-sloppy")
set_flag("harness.plan_json_repair.enabled", True)
check("E20-08", (s1, on["status"]["state"], off["status"]["state"], "JSON" in json.dumps(off["status"], ensure_ascii=False)),
      (200, "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", True))

# E20-09 ablation — 보정 2종을 하나씩·전부 끈 변형 비교 (HARN-05)
AB = {**GATE, "name": f"ab-{N}", "targets": [{**GATE["targets"][0], "data_template": {"goal": "{{input.goal}}", "model_alias": "planner-sloppy"}}]}
ab = j(c.post(f"{BL}/v1/ablations", json={"suite": AB, "flags": ["harness.plan_json_repair.enabled", "harness.plan_heuristic_fallback.enabled"]}))
rec = {v["variant"]: v["recommendation"] for v in ab.get("variants", [])}
check("E20-09", (ab.get("baseline", {}).get("metrics", {}).get("pass_rate"), rec),
      (1.0, {"off:harness.plan_json_repair.enabled": "removable", "off:harness.plan_heuristic_fallback.enabled": "removable", "all-off": "keep"}))

# E20-10 하네스 매니페스트 → AL-RUN 루프 설정 (HARN-03) · 규칙 외부화 (HARN-02)
h1 = j(c.post(f"{BL}/v1/harness/al-run", json={"manifest": {"loop": {"max_turns": 1}}}))
h2 = j(c.post(f"{BL}/v1/harness/al-run", json={"manifest": {"loop": {"max_turns": 25}}}))
tok = {"authorization": f"Bearer {ADMIN}"}
c.post(f"{BL}/v1/harness/al-run/labels/production", headers=tok, json={"version": h1["version"], "override": True, "reason": "e20 루프 한도 시험"})
time.sleep(11)
RUN = next(a["agentId"] for a in c.get(f"{M}/api/v1/a2a/registry/agents", params={"limit": 50}, headers=MH).json()["items"] if a["name"] == "al-run")
t = c.post(f"{M}/api/v1/agents/{RUN}/a2a", headers=MH, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
    "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": {"goal": f'atlas_calc({{"expr":"2+3"}}) 로 계산 {N}'}}]}}}).json()["result"]["task"]
c.post(f"{BL}/v1/harness/al-run/labels/production", headers=tok, json={"version": h2["version"], "override": True, "reason": "e20 복원"})
res = ((t.get("artifacts") or [{}])[0].get("parts") or [{}])[0].get("data", {})
reason = res.get("reason") or json.dumps(t["status"], ensure_ascii=False)
j(c.post(f"{BL}/v1/rules/refund-{N}", json={"yaml": "rules:\n- {id: big, when: 'amount > 100000', action: require_approval}\n"}))
rv = j(c.post(f"{BL}/v1/rules/refund-{N}/evaluate", params={"label": "staging"}, json={"facts": {"amount": 200000}}))
check("E20-10", ("MAX_TURNS" in reason or "BUDGET" in reason or "turn" in reason.lower(), rv.get("decision")), (True, "require_approval"))

# E20-11 반복 샘플링 + 검증기 (COMP-03) · 품질-계산 곡선 (COMP-06)
bon = j(c.post(f"{BL}/v1/compute/best-of-n", json={"model": "chat-default", "messages": [{"role": "user", "content": f"숫자 {N}"}], "n": 8,
                                                  "verifier": {"type": "regex", "pattern": r"\[s=[0-4]\]"}}))
CV = {**SUITE, "name": f"curve-{N}", "targets": [{"id": "r", "type": "prompt", "model": "reasoner", "template": "{{input.q}}"}]}
cv = j(c.post(f"{BL}/v1/compute/curve", json={"suite": CV, "efforts": ["low", "medium", "high"]}))
costs = {p["effort"]: p["cost_usd"] for p in cv.get("points", [])}
check("E20-11", (bon.get("status"), len(bon.get("samples", [])) == (bon.get("chosen", -9) + 1), costs.get("low", 9) < costs.get("high", 0),
                 cv.get("recommended", {}).get("effort")), ("verified", True, True, "low"))

# E20-12 이벤트 — plt.bl.* 가 Manager 중계로 PLT 스트림에 (알림 규칙 대상)
try:
    out = subprocess.run(["docker", "run", "--rm", "--network", NET, "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222", "stream", "subjects", "PLT", "--json"],
                         capture_output=True, text=True, timeout=90).stdout
    seen = json.loads(out or "{}")
except (subprocess.SubprocessError, ValueError, FileNotFoundError):
    seen = {}
check("E20-12", tuple(s in seen for s in ("plt.bl.model.candidate_added", "plt.bl.model.promoted", "plt.bl.model.rolled_back", "plt.bl.prompt.published")),
      (True, True, True, True))

c.delete(f"{G}/admin/aliases/{ALIAS}", headers=GA)
c.delete(f"{G}/admin/aliases/{ALIAS}-prev", headers=GA)
print("== failed", failed)
sys.exit(1 if failed else 0)
