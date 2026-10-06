"""Epic 19 Judge·점수 스택 시험 — 실제 Gateway mock judge · EV · OB(ClickHouse) 로 judge 정의→보정→채점 스위트→diff→shadow→온라인→피드백.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/19th/e19_judge.py <local|prod>
env: ATLAS_GATEWAY_ADMIN_TOKEN · ATLAS_GATEWAY_AL_PLAN_KEY · ATLAS_EV_CURATOR_TOKEN"""
import json
import os
import secrets
import sys
import time

import httpx

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
EV, G, OB = "http://127.0.0.1:8007", "http://127.0.0.1:8090", "http://127.0.0.1:8006"
ADMIN, KEY, CUR = os.environ["ATLAS_GATEWAY_ADMIN_TOKEN"], os.environ["ATLAS_GATEWAY_AL_PLAN_KEY"], os.environ["ATLAS_EV_CURATOR_TOKEN"]
N = secrets.token_hex(3)
c = httpx.Client(timeout=120)
failed = 0
RUBRIC = "환불 응답 품질 루브릭\n- 포함: 환불, 영업일\n- 금지: 욕설"


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def judge(name, **kw):
    r = c.post(f"{EV}/v1/judges", json={"name": name, "rubric_md": RUBRIC, **kw})
    r.raise_for_status()
    return r.json()


def ev_eval(name, body, **q):
    return c.post(f"{EV}/v1/judges/{name}/evaluate", params=q, json=body).json()


# E19-01 루브릭 judge 버전 · binary + critique (JUDGE-01·02)
j1 = judge(f"refund-{N}")
j2 = judge(f"refund-{N}", rubric_md=RUBRIC + "\n- 포함: 3")
ok = ev_eval(f"refund-{N}", {"output": "환불은 3 영업일 안에 처리됩니다"}, version=1)
bad = ev_eval(f"refund-{N}", {"output": "확인해 보겠습니다"}, version=1)
check("E19-01", (j1["version"], j2["version"], ok["verdict"], bad["verdict"], "필수 표현" in bad["critique"]), (1, 2, "pass", "fail", True))

# E19-02 pairwise 위치 교차 (JUDGE-03)
judge(f"pw-{N}", mode="pairwise")
judge(f"pwb-{N}", mode="pairwise", model="judge-biased")
p = ev_eval(f"pw-{N}", {"output_a": "모름", "output_b": "환불은 3 영업일"})
pb = ev_eval(f"pwb-{N}", {"output_a": "모름", "output_b": "환불은 3 영업일"})
check("E19-02", (p["winner"], p["position_consistent"], pb["winner"], pb["position_consistent"]), ("B", True, "tie", False))

# E19-03 배심원 (JUDGE-04)
judge(f"jury-{N}", jury=["judge-default", "judge-lenient", "judge-length"])
jr = ev_eval(f"jury-{N}", {"output": "환불 요청을 접수했습니다. 곧 연락드리겠습니다"})
check("E19-03", (jr["verdict"], len(jr.get("votes", [])), jr.get("agreement")), ("pass", 3, 0.6667))

# E19-04 사람 일치도 보정 (JUDGE-05)
items = [{"output": "환불은 3 영업일 안에 처리됩니다", "human": "pass"}] * 5 + [{"output": "모르겠습니다", "human": "fail"}] * 5
cal = c.post(f"{EV}/v1/judges/refund-{N}/calibrate", params={"version": 1}, json={"items": items}).json()
judge(f"len-{N}", model="judge-length")
cal2 = c.post(f"{EV}/v1/judges/len-{N}/calibrate", json={"items": items}).json()
check("E19-04", (cal["kappa"], cal["trusted"], cal2["trusted"], cal2["kappa"] < 0.6), (1.0, True, False, True))

# E19-05 judge 채점 스위트 · 미보정 judge 게이트 차단 (JUDGE-01·05 · EV-REG)
ds = f"refund-qa-{N}"
c.post(f"{EV}/v1/datasets", json={"name": ds})
for q in ("환불 기간?", "환불 언제 돼요?", "환불 규정", "환불 처리 기간", "환불 문의"):
    it = c.post(f"{EV}/v1/datasets/{ds}/items", json={"input": {"q": q}}).json()
    c.post(f"{EV}/v1/items/{it['id']}/transition", json={"to": "in_review"})
    c.post(f"{EV}/v1/items/{it['id']}/transition", json={"to": "approved", "verdict": "pass"}, headers={"authorization": f"Bearer {CUR}"})
c.post(f"{EV}/v1/datasets/{ds}/versions")
base = {"name": f"refund-{N}", "dataset": {"name": ds}, "thresholds": {"pass_rate": 0.9},
        "scorers": [{"type": "judge", "judge": f"refund-{N}", "version": 1}]}


def suite(tid, template, **over):
    return {**base, "targets": [{"id": tid, "type": "prompt", "model": "chat-default", "template": template}], **over}


r1 = c.post(f"{EV}/v1/runs", content=json.dumps(suite("prompt-v1", "{{input.q}} " + N))).json()
r2 = c.post(f"{EV}/v1/runs", content=json.dumps(suite("prompt-v2", "환불은 3 영업일 안에 처리됩니다 — {{input.q}} " + N))).json()
ru = c.post(f"{EV}/v1/runs", content=json.dumps(suite("prompt-v2", "환불은 3 영업일 — {{input.q}} " + N, scorers=[{"type": "judge", "judge": f"len-{N}"}]))).json()
check("E19-05", (r1["gate"], r1["summary"]["pass_rate"], r2["gate"], r2["summary"]["pass_rate"], ru["gate"], "미보정" in (ru["summary"]["gate_reasons"] or [""])[0]),
      ("fail", 0.0, "pass", 1.0, "fail", True))

# E19-06 실험 diff · 유의성 (SCORE-02·03)
d = c.get(f"{EV}/v1/runs/{r1['run_id']}/diff/{r2['run_id']}").json()
rd = c.get(f"{EV}/v1/runs/{r2['run_id']}/diff/{r1['run_id']}").json()
check("E19-06", (d["matrix"]["fail_pass"], d["verdict"], d["mcnemar_p"], d["ci95"], rd["verdict"], rd["items"][0]["change"]),
      (5, "improved", 0.0625, [1.0, 1.0], "regressed", "regression"))

# E19-07 criteria drift shadow (JUDGE-06)
judge(f"refund-{N}", rubric_md=RUBRIC + "\n- 포함: 상담원")
s2 = c.post(f"{EV}/v1/judges/refund-{N}/shadow", json={"from_version": 1, "to_version": 2}).json()
s3 = c.post(f"{EV}/v1/judges/refund-{N}/shadow", json={"from_version": 1, "to_version": 3}).json()
check("E19-07", (s2["sampled"], s2["change_rate"], s3["pass_to_fail"], s3["change_rate"]), (10, 0.0, 5, 0.5))

# E19-08 RAG judge (JUDGE-08)
judge(f"rag-{N}", mode="rag", rubric_md="근거 충실도", threshold=0.9)
rg = ev_eval(f"rag-{N}", {"output": "환불 기한은 7일이다. 배송은 무료이다.", "contexts": ["고객 환불 기한은 7일이다."], "retrieved_ids": ["d1", "d9"], "relevant_ids": ["d1", "d2"]})
check("E19-08", (rg["faithfulness"], rg["passed"], rg["context_precision"], rg["context_recall"]), (0.5, False, 0.5, 0.5))

# E19-09 온라인 judge 샘플링 → 저점 trace 골든셋 후보 (JUDGE-09 · SCORE-07)
tenant = f"e19-{N}"
vk = c.post(f"{G}/admin/keys", json={"tenant": tenant, "models": ["*"]}, headers={"Authorization": f"Bearer {ADMIN}"}).json()["key"]
tids = []
for msg in ("환불은 3 영업일 안에 처리됩니다", "환불 문의 고객 010-2222-3333 답변 불가", "모르겠습니다"):
    h = c.post(f"{G}/v1/chat/completions", json={"model": "chat-default", "messages": [{"role": "user", "content": f"{msg} {N}"}]},
               headers={"Authorization": f"Bearer {vk}"}).headers
    tids.append(h["x-atlas-trace-id"])
for t in tids:
    for _ in range(40):
        if c.get(f"{OB}/v1/traces/{t}/io").json().get("captured"):
            break
        time.sleep(1)
judge(f"online-{N}")
cands_ds = f"cands-{N}"
rule = c.post(f"{EV}/v1/online-rules", json={"name": f"e19-{N}", "filter": f'atlas.tenant = "{tenant}"', "sample_rate": 1.0, "judge": f"online-{N}",
                                            "dataset": cands_ds, "tenant": tenant, "enabled": False}).json()
o = c.post(f"{EV}/v1/online-rules/{rule['id']}/run").json()
again = c.post(f"{EV}/v1/online-rules/{rule['id']}/run").json()
cands = c.get(f"{EV}/v1/datasets/{cands_ds}/items").json().get("items", [])
masked = all("010-2222-3333" not in json.dumps(x, ensure_ascii=False) for x in cands)
check("E19-09", (o["fetched"], o["scored"], o["failed"], len(o["candidates"]), again["scored"], {x["status"] for x in cands}, masked),
      (3, 3, 2, 2, 0, {"in_review"}, True))

# E19-10 사용자 피드백 (SCORE-08)
fb = c.post(f"{EV}/v1/feedback", json={"trace_id": tids[0], "value": "down", "comment": "e19 부정", "dataset": cands_ds, "tenant": tenant}).json()
fs = c.get(f"{EV}/v1/scores", params={"trace_id": tids[0]}).json()["items"]
check("E19-10", (fb["negative"], bool(fb["candidate_item"]), sorted(x["source"] for x in fs)), (True, True, ["feedback", "online"]))

# (템플릿에 실행별 nonce — 같은 요청은 Gateway exact 캐시가 비용 0 으로 응답하므로)
# E19-11 추세 · 드리프트 · 스코어카드 (SCORE-04·05·09)
tr = c.get(f"{EV}/v1/scores/trend", params={"suite": f"refund-{N}"}).json()["points"]
dr = c.get(f"{EV}/v1/scores/drift", params={"scorer": f"judge:online-{N}", "baseline": 2}).json()
card = {x["target"]: x for x in c.get(f"{EV}/v1/scorecard", params={"suite": f"refund-{N}"}).json()["items"]}
check("E19-11", ([p["pass_rate"] for p in tr], "drift" in dr, set(card), card["prompt-v1"]["task_success_rate"], card["prompt-v1"]["cost_per_task_usd"] > 0),
      ([0.0, 1.0, 1.0], True, {"prompt-v1", "prompt-v2"}, 0.0, True))

# E19-12 judge 비용 귀속 (Gateway 태그 unit_id:EV-JUDGE → OB 비용)
cost = 0
for _ in range(30):
    rows = c.get(f"{OB}/v1/costs", params={"group_by": "agent", "since": 3600}).json().get("items", [])
    cost = next((r["cost_usd"] for r in rows if r["grp"] == "EV-JUDGE"), 0)
    if cost:
        break
    time.sleep(2)
check("E19-12", cost > 0, True)
print("== failed", failed)
sys.exit(1 if failed else 0)
