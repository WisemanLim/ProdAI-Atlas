"""Epic 21 성숙도 스택 시험 — 실제 플랫폼 텔레메트리로 MAT 를 진단한다. 실행별 프로젝트(e21-<nonce>)로 스냅샷 이력을 분리하고,
게이트가 요구하는 운영 사실(골든셋·회귀·judge·온라인·예산·캐시·계산 정책·SLO·경보·가드·승인·후보·프롬프트)을 만들어 단계가 오르는지 본다.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/21st/e21_mat.py <local|prod>"""
import json
import os
import secrets
import subprocess
import sys
import time
import uuid

import httpx

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
MAT, EV, G, OB, GR, BL = ("http://127.0.0.1:8017", "http://127.0.0.1:8007", "http://127.0.0.1:8090", "http://127.0.0.1:8006", "http://127.0.0.1:8005",
                          "http://127.0.0.1:8015")
M = os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")
ADMIN, CUR = os.environ["ATLAS_GATEWAY_ADMIN_TOKEN"], os.environ["ATLAS_EV_CURATOR_TOKEN"]
MH = {"Authorization": f"Bearer {os.environ['ATLAS_TEST_TOKEN']}"} if os.environ.get("ATLAS_TEST_TOKEN") else {}
GA = {"Authorization": f"Bearer {ADMIN}"}
N = secrets.token_hex(3)
P = f"e21-{N}"
c = httpx.Client(timeout=300)
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def assess():
    return c.get(f"{MAT}/v1/assessment", params={"project": P, "fresh": True}).json()


# 시작 상태 스냅샷 (이 프로젝트 이력의 기준선)
s0 = c.post(f"{MAT}/v1/snapshots", params={"project": P}).json()
a0 = assess()

# ---- 운영 사실 만들기 ----
vk = c.post(f"{G}/admin/keys", headers=GA, json={"tenant": "default", "project": P, "models": ["*"]}).json()
KEY = vk["key"]
budgets = [f"e21-org-{N}", f"e21-key-{N}"]
c.put(f"{G}/admin/budgets/{budgets[0]}", headers=GA, json={"scope": "project", "scope_id": f"default/{P}", "amount_usd": "1000", "period": "monthly", "hard_action": "block"})
c.put(f"{G}/admin/budgets/{budgets[1]}", headers=GA, json={"scope": "key", "scope_id": vk["virtualKey"]["prefix"], "amount_usd": "1000", "period": "monthly", "hard_action": "block"})
c.put(f"{G}/admin/compute-policies/e21-{N}", headers=GA, json={"match": {"agent": f"E21-{N}"}, "effort": "auto"})
# prompt cache: 긴 공통 접두(시스템 프롬프트)를 반복 → 읽기 토큰
system = ("너는 Atlas 고객 지원 상담원이다. 정책 문서를 근거로만 답한다. " * 250)
traces = []
for i in range(6):
    r = c.post(f"{G}/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json={"model": "chat-default", "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": f"환불 문의 {i} {N}"}]})
    traces.append(r.headers.get("x-atlas-trace-id"))
# 골든셋 50 + 통과 회귀 10회
ds = f"gold-{N}"
c.post(f"{EV}/v1/datasets", json={"name": ds})
lines = "\n".join(json.dumps({"input": {"q": f"질문 {i} {N}"}}) for i in range(50))
c.post(f"{EV}/v1/datasets/{ds}/items:bulk", content=lines)
for it in c.get(f"{EV}/v1/datasets/{ds}/items").json()["items"]:
    c.post(f"{EV}/v1/items/{it['id']}/transition", json={"to": "in_review"})
    c.post(f"{EV}/v1/items/{it['id']}/transition", json={"to": "approved", "verdict": "pass"}, headers={"authorization": f"Bearer {CUR}"})
c.post(f"{EV}/v1/datasets/{ds}/versions")
suite = {"name": f"e21-{N}", "dataset": {"name": ds}, "scorers": [{"type": "contains", "value": "[mock:"}], "thresholds": {"pass_rate": 0.9}, "max_concurrency": 16,
         "targets": [{"id": "prod", "type": "prompt", "model": "chat-default", "template": "{{input.q}}"}]}
for _ in range(10):
    c.post(f"{EV}/v1/runs", content=json.dumps(suite))
# judge 보정 · 온라인 규칙
c.post(f"{EV}/v1/judges", json={"name": f"e21-{N}", "rubric_md": "- 포함: 환불"})
c.post(f"{EV}/v1/judges/e21-{N}/calibrate", json={"items": [{"output": "환불됩니다", "human": "pass"}] * 3 + [{"output": "몰라요", "human": "fail"}] * 3})
c.post(f"{EV}/v1/online-rules", json={"name": f"e21-{N}", "filter": f'atlas.tenant = "e21-none-{N}"', "sample_rate": 0.1, "judge": f"e21-{N}", "enabled": True})
# trace → 골든셋 승격 (긴 시스템 프롬프트 요청은 본문 캡처 상한으로 미수집 — 짧은 요청을 따로)
traces[0] = c.post(f"{G}/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                   json={"model": "chat-default", "messages": [{"role": "user", "content": f"배송 문의 {N}"}]}).headers.get("x-atlas-trace-id")
for _ in range(40):
    if c.get(f"{OB}/v1/traces/{traces[0]}/io").json().get("captured"):
        break
    time.sleep(1)
c.post(f"{EV}/v1/datasets", json={"name": f"promo-{N}", "input_schema": {"type": "object", "required": ["prompt"]}})
promo = c.post(f"{EV}/v1/datasets/promo-{N}/items:from-trace", json={"trace_id": traces[0]})
# SLO · 경보 · 가드 · 승인
slo = c.post(f"{OB}/v1/slos", json={"name": f"e21-{N}", "kind": "error_rate", "objective": 0.99}).json()
c.post(f"{OB}/v1/alerts/rules", json={"name": f"e21-{N}", "expr": {"type": "metric", "metric": "error_rate", "op": ">", "value": 0.5, "window": "1h"}})
c.post(f"{GR}/v1/guard/check", json={"tenant": "default", "boundary": "input", "contents": [f"고객 900101-1234568 문의 {N}"]})
reg = {a["name"]: a["agentId"] for a in c.get(f"{M}/api/v1/a2a/registry/agents", params={"limit": 50}, headers=MH).json()["items"]}
ap = c.post(f"{M}/api/v1/approvals", params={"org": "default"}, headers=MH, json={"source": "tool", "sourceRef": f"e21-{N}", "agentId": reg["al-run"],
                                                                                  "action": "e21.check", "riskLevel": "low", "payload": {}}).json()
c.post(f"{M}/api/v1/approvals/{ap['id']}/decision", params={"org": "default"}, headers=MH, json={"decision": "reject", "reason": "e21"})
# 모델 교체 후보(회귀 0) · 프롬프트 production
alias = f"e21-chat-{N}"
c.put(f"{G}/admin/aliases/{alias}", headers=GA, json={"deployments": [{"provider": "mock", "model": "mock-chat", "weight": 1}]})
cand = c.post(f"{BL}/v1/models/mock-chat-small/candidate", json={"alias": alias, "suites": [{**suite, "name": f"e21c-{N}", "targets": [{**suite["targets"][0], "model": alias}]}], "k": 1}).json()
c.post(f"{BL}/v1/prompts/e21.system-{N}", json={"template": "you are e21"})
c.post(f"{BL}/v1/prompts/e21.system-{N}/labels/production", headers=GA, json={"version": 1, "override": True, "reason": "e21 성숙도 시험"})

# ---- 판정 ----
a1 = assess()
st = a1["stage"]
# E21-01 4속성 점수 (MAT-01) — 증거 있는 판정, 출처 오류 없음
scores = {k: v["score"] for k, v in a1["attributes"].items()}
check("E21-01", (all(v >= 70 for v in scores.values()), a1["source_errors"], all(ch["result"] in ("pass", "fail", "unknown") for v in a1["attributes"].values() for ch in v["checks"])),
      (True, {}, True))
# E21-02 단계 L0~L4 · 연속 게이트 (MAT-02)
check("E21-02", (st["level"], st["name"], a1["band"]["phase"], [lv["passed"] for lv in st["levels"]]), (4, "Autonomous Multi-agent", "Compound", [True, True, True, True]))
# E21-03 9개념(+A2A) 커버리지 (MAT-03)
cov = a1["coverage"]
check("E21-03", (cov["total"], cov["adopted"] >= 8, cov["matrix"]["01-agentic-loops"]["reliable"], cov["matrix"]["03-multi-agent"]["safe"]), (10, True, "●", "●"))
# E21-04 복리 루프 (MAT-04)
cp = a1["compound"]
# 후보 판정 자체(promote·hold)는 실행 시 지연 노이즈에 따라 달라질 수 있다 — 성숙도 게이트는 "판정 존재 + 회귀 0" 만 본다
check("E21-04", (promo.status_code, cp["trace_promotions_30d"] >= 1, cp["model_swap_regressions"], cp["loop_running"], cand.get("status"),
                 cand.get("decision") in ("promote", "hold", "review-cost", "insufficient-evidence")), (201, True, 0, True, "decided", True))
# E21-05 로드맵 (MAT-05) — 시작 시점(미달 게이트 있음)과 지금(없음)
check("E21-05", (len(a1["roadmap"]["cards"]), all(cd["role"] in ("platform", "ai_qa", "security", "finops") for cd in a0["roadmap"]["cards"]),
                 a0["stage"]["level"] <= a1["stage"]["level"]), (0, True, True))
# E21-06 공통 지표 정의 (MAT-06) — 7종, 값 있음
mt = {m["name"]: m["value"] for m in a1["metrics"]}
check("E21-06", (sorted(mt), mt["cache_hit_rate"] >= 0.2, mt["task_success_rate"] is not None, mt["hitl_approval_latency"] is not None),
      (sorted(["task_success_rate", "cost_per_task", "cache_hit_rate", "tool_error_rate", "guardrail_block_rate", "hitl_approval_latency", "turns_to_completion"]),
       True, True, True))
# E21-07 스냅샷·리포트·단계 변경 이벤트 (MAT-08)
s1 = c.post(f"{MAT}/v1/snapshots", params={"project": P}).json()
rep = c.get(f"{MAT}/v1/report", params={"project": P}).text
hist = c.get(f"{MAT}/v1/snapshots", params={"project": P}).json()["items"]
check("E21-07", (len(hist), s1["level"], "L4 Autonomous Multi-agent" in rep, "## 조직 롤업" in rep), (2, 4, True, True))
# E21-08 홈 진화 띠 (MAT-09) — 콘솔 경로(Manager /svc/mat)
band = c.get(f"{M}/svc/mat/v1/band", params={"project": P}, headers=MH).json()
check("E21-08", (band["stage"], band["phase"], band["progress_pct"], band["unmet_gates"], set(band["score_by_attr"])),
      (4, "Compound", 100, [], {"Reliable", "Observable", "Cost-aware", "Safe"}))
# E21-09 성과 지표 (MAT-10) — 기준선 = 첫 스냅샷
oc = {x["name"]: x for x in a1["outcomes"]["cards"]}
check("E21-09", (set(oc), a1["outcomes"]["baseline_at"] == s0["created_at"], oc["Lower cost"]["current"] is not None),
      ({"Better decisions", "Lower cost", "Faster wins"}, True, True))
# E21-10 단계 변경 이벤트 → PLT 스트림 (알림 대상) — 기준선과 단계가 다를 때
seen = {}
if s0["level"] != s1["level"]:
    out = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222",
                          "stream", "subjects", "PLT", "--json"], capture_output=True, text=True, timeout=90).stdout
    seen = json.loads(out or "{}")
check("E21-10", s0["level"] == s1["level"] or "plt.mat.stage_changed" in seen, True)
# E21-11 A2A skill mat.assess
t = c.post(f"{M}/api/v1/agents/{reg['mat']}/a2a", headers=MH, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
    "messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": {"project": P}}]}, "metadata": {"skill": "mat.assess"}}}).json()["result"]["task"]
check("E21-11", (t["status"]["state"], t["artifacts"][0]["parts"][0]["data"]["band"]["stage"]), ("TASK_STATE_COMPLETED", 4))

# 정리 — 다른 시험에 영향 없게
for b in budgets:
    c.delete(f"{G}/admin/budgets/{b}", headers=GA)
c.delete(f"{G}/admin/compute-policies/e21-{N}", headers=GA)
if cand.get("id"):
    c.post(f"{BL}/v1/candidates/{cand['id']}/rollback", json={"by": "e21", "reason": "정리"})
c.delete(f"{G}/admin/aliases/{alias}", headers=GA)
print("== failed", failed)
sys.exit(1 if failed else 0)
