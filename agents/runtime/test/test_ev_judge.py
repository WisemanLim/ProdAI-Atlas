"""EV-JUDGE · EV-SCORE — 루브릭 judge·pairwise 위치 교차·배심원·보정 κ·shadow·RAG·judge 채점 스위트·diff·추세·드리프트·온라인·피드백·스코어카드."""
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas_agent_core import create_app
from units import ev
from units.ev import stats

CURATOR = {"authorization": "Bearer test-curator"}


def sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^## ([A-Z][A-Z ]*)\s*$", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def rules(rubric):
    must, forbid = [], []
    for kind, words in re.findall(r"(?im)^\s*-?\s*(포함|금지)\s*:\s*(.+)$", rubric):
        (must if kind == "포함" else forbid).extend(w.strip().lower() for w in words.split(","))
    return must, forbid


def fake_judge(model, text):
    """Gateway mock judge(Go) 와 같은 규칙의 시험용 축약판."""
    s = sections(text)
    must, forbid = rules(s.get("RUBRIC", ""))
    hit = lambda o: sum(w in o.lower() for w in must)
    bad = lambda o: sum(w in o.lower() for w in forbid)
    mode = s.get("MODE", "binary")
    if mode == "pairwise":
        a, b = s.get("OUTPUT A", ""), s.get("OUTPUT B", "")
        if model == "judge-biased":
            return {"winner": "A"}
        sa, sb = hit(a) - 2 * bad(a), hit(b) - 2 * bad(b)
        return {"winner": "A" if sa > sb else "B" if sb > sa else "tie"}
    if mode == "claim":
        toks = [t for t in re.split(r"[^\w]+", s.get("CLAIM", "").lower()) if len(t) >= 2]
        ctx = s.get("CONTEXT", "").lower()
        r = sum(t in ctx for t in toks) / max(1, len(toks))
        return {"supported": r >= 0.6, "score": r}
    o = s.get("OUTPUT", "")
    ok = bad(o) == 0 and (hit(o) > 0 if model == "judge-lenient" else len(o) >= 20 if model == "judge-length" else hit(o) == len(must))
    return {"verdict": "pass" if ok else "fail", "score": hit(o) / max(1, len(must)), "critique": f"{model} {hit(o)}/{len(must)}"}


OB_TRACES = {"t-good": ("환불 규정?", "환불은 3 영업일 안에 처리됩니다"), "t-bad": ("환불 규정?", "모르겠어요"),
             "t-bad2": ("환불?", "글쎄요"), "t-fb": ("배송?", "배송은 내일")}


def handler(req: httpx.Request) -> httpx.Response:
    url = str(req.url)
    if "/v1/chat/completions" in url:
        body = json.loads(req.content)
        user = body["messages"][-1]["content"]
        if body["model"].startswith("judge"):
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(fake_judge(body["model"], user), ensure_ascii=False)}}]},
                                  headers={"x-atlas-cost-usd": "0.0001"})
        # 피평가 타깃: 프롬프트 v1 은 규정을 모르고 v2 는 안다
        ans = "환불은 3 영업일 안에 처리됩니다" if "v2" in body["messages"][0]["content"] else "확인해 보겠습니다"
        return httpx.Response(200, json={"choices": [{"message": {"content": ans}}]}, headers={"x-atlas-cost-usd": "0.001"})
    if url.endswith("/io"):
        tid = url.split("/v1/traces/")[1].split("/")[0]
        i, o = OB_TRACES[tid]
        return httpx.Response(200, json={"trace_id": tid, "captured": True, "input": i, "output": o})
    if "/v1/traces" in url:
        return httpx.Response(200, json={"items": [{"trace_id": t} for t in ("t-good", "t-bad", "t-bad2")]})
    return httpx.Response(404, json={})


@pytest.fixture(scope="module")
def evc(client):
    import os

    from app import db
    os.environ["ATLAS_EV_CURATOR_TOKEN"] = "test-curator"
    ev.TRANSPORT = httpx.MockTransport(handler)
    with TestClient(create_app(ev.agent, engine=db.engine, public_url="http://ev:8000")) as c:
        yield c
    ev.TRANSPORT = None


RUBRIC = "환불 응답 품질\n- 포함: 환불, 영업일\n- 금지: 욕설"


def test_judge_versions_binary_pairwise_jury_rag(evc):
    v1 = evc.post("/v1/judges", json={"name": "refund", "rubric_md": RUBRIC}).json()
    v2 = evc.post("/v1/judges", json={"name": "refund", "rubric_md": RUBRIC + "\n- 포함: 3"}).json()
    assert (v1["version"], v2["version"]) == (1, 2) and evc.get("/v1/judges/refund?version=1").json()["rubric_md"] == RUBRIC
    ok = evc.post("/v1/judges/refund/evaluate?version=1", json={"output": "환불은 3 영업일 안에"}).json()
    bad = evc.post("/v1/judges/refund/evaluate?version=1", json={"output": "모름"}).json()
    assert (ok["verdict"], bad["verdict"], ok["critique"] != "") == ("pass", "fail", True)
    # pairwise: 위치 교차 — 일반 judge 는 일관, 편향 judge(항상 A) 는 불일치 → tie
    evc.post("/v1/judges", json={"name": "pw", "mode": "pairwise", "rubric_md": RUBRIC})
    evc.post("/v1/judges", json={"name": "pw-biased", "mode": "pairwise", "rubric_md": RUBRIC, "model": "judge-biased"})
    p = evc.post("/v1/judges/pw/evaluate", json={"output_a": "모름", "output_b": "환불은 3 영업일"}).json()
    pb = evc.post("/v1/judges/pw-biased/evaluate", json={"output_a": "모름", "output_b": "환불은 3 영업일"}).json()
    assert (p["winner"], p["position_consistent"], pb["winner"], pb["position_consistent"]) == ("B", True, "tie", False)
    # 배심원 3: strict(fail) · lenient(pass) · length(pass) → 다수결 pass, 일치도 2/3
    assert evc.post("/v1/judges", json={"name": "j2", "rubric_md": RUBRIC, "jury": ["judge-default", "judge-lenient"]}).status_code == 422
    evc.post("/v1/judges", json={"name": "jury", "rubric_md": RUBRIC, "jury": ["judge-default", "judge-lenient", "judge-length"]})
    jr = evc.post("/v1/judges/jury/evaluate", json={"output": "환불 요청을 접수했습니다 곧 연락드립니다"}).json()
    assert (jr["verdict"], len(jr["votes"]), jr["agreement"]) == ("pass", 3, 0.6667)
    # RAG faithfulness · context precision/recall
    evc.post("/v1/judges", json={"name": "rag", "mode": "rag", "rubric_md": "근거 충실도", "threshold": 0.9})
    r = evc.post("/v1/judges/rag/evaluate", json={"output": "환불 기한은 7일이다. 배송은 무료이다.", "contexts": ["고객 환불 기한은 7일이다."],
                                                  "retrieved_ids": ["d1", "d9"], "relevant_ids": ["d1", "d2"]}).json()
    assert (r["faithfulness"], r["passed"], r["context_precision"], r["context_recall"]) == (0.5, False, 0.5, 0.5)


def test_calibration_gate_and_shadow(evc):
    evc.post("/v1/judges", json={"name": "cal", "rubric_md": RUBRIC})
    good, bad = "환불은 3 영업일 안에 처리", "모르겠습니다"
    items = [{"output": good, "human": "pass"}] * 4 + [{"output": bad, "human": "fail"}] * 4
    c = evc.post("/v1/judges/cal/calibrate", json={"items": items}).json()
    assert (c["kappa"], c["agreement"], c["trusted"]) == (1.0, 1.0, True)
    off = evc.post("/v1/judges", json={"name": "cal-off", "rubric_md": RUBRIC, "model": "judge-length"}).json()
    c2 = evc.post("/v1/judges/cal-off/calibrate", json={"items": items}).json()
    assert c2["trusted"] is False and c2["kappa"] < 0.6 and off["version"] == 1
    # judge 채점 스위트: 보정된 judge 로 v1·v2 프롬프트 비교 → 미보정 judge 는 게이트 실패
    evc.post("/v1/datasets", json={"name": "refund-qa"})
    for q in ("환불 기간?", "환불 언제 돼요?", "환불 규정 알려줘", "환불 처리 기간"):
        it = evc.post("/v1/datasets/refund-qa/items", json={"input": {"q": q}}).json()
        evc.post(f"/v1/items/{it['id']}/transition", json={"to": "in_review"})
        evc.post(f"/v1/items/{it['id']}/transition", json={"to": "approved", "verdict": "pass"}, headers=CURATOR)
    evc.post("/v1/datasets/refund-qa/versions")
    suite = {"name": "refund", "dataset": {"name": "refund-qa"}, "scorers": [{"type": "judge", "judge": "cal"}], "thresholds": {"pass_rate": 0.9}}
    r1 = evc.post("/v1/runs", content=json.dumps({**suite, "targets": [{"id": "prompt-v1", "type": "prompt", "model": "chat-default", "system": "v1", "template": "{{input.q}}"}]})).json()
    r2 = evc.post("/v1/runs", content=json.dumps({**suite, "targets": [{"id": "prompt-v2", "type": "prompt", "model": "chat-default", "system": "v2", "template": "{{input.q}}"}]})).json()
    assert (r1["gate"], r1["summary"]["pass_rate"], r2["gate"], r2["summary"]["pass_rate"]) == ("fail", 0.0, "pass", 1.0)
    u = evc.post("/v1/runs", content=json.dumps({**suite, "scorers": [{"type": "judge", "judge": "cal-off"}],
                                                   "targets": [{"id": "prompt-v2", "type": "prompt", "model": "chat-default", "system": "v2", "template": "{{input.q}}"}]})).json()
    assert u["gate"] == "fail" and "미보정" in u["summary"]["gate_reasons"][0]
    sc = evc.get("/v1/scores", params={"run_id": r2["run_id"]}).json()["items"]
    assert len(sc) == 4 and all(x["scorer"] == "judge:cal" and x["judge_version"] == 1 and x["passed"] for x in sc)
    # diff: v1 → v2 개선 4건, McNemar · CI
    d = evc.get(f"/v1/runs/{r1['run_id']}/diff/{r2['run_id']}").json()
    assert (d["matrix"]["fail_pass"], d["delta_pass_rate"], d["verdict"], d["ci95"]) == (4, 1.0, "improved", [1.0, 1.0])
    assert d["mcnemar_p"] == 0.125 and d["significant"] is False      # 4건 → 유의하지 않음 (표본 작음)
    rd = evc.get(f"/v1/runs/{r2['run_id']}/diff/{r1['run_id']}").json()
    assert rd["verdict"] == "regressed" and rd["items"][0]["change"] == "regression"
    # shadow: 루브릭 개정(v2: "3" 추가)으로 재채점 → 변경 0 (모두 3 포함), 더 엄격한 v3 → 전부 pass→fail
    evc.post("/v1/judges", json={"name": "cal", "rubric_md": RUBRIC + "\n- 포함: 3"})
    evc.post("/v1/judges", json={"name": "cal", "rubric_md": RUBRIC + "\n- 포함: 상담원"})
    s2 = evc.post("/v1/judges/cal/shadow", json={"from_version": 1, "to_version": 2}).json()
    s3 = evc.post("/v1/judges/cal/shadow", json={"from_version": 1, "to_version": 3}).json()
    assert (s2["sampled"], s2["change_rate"]) == (8, 0.0) and (s3["change_rate"], s3["pass_to_fail"]) == (0.5, 4)
    tr = evc.get("/v1/scores/trend", params={"suite": "refund", "scorer": "judge:cal"}).json()["points"]
    assert [p["pass_rate"] for p in tr][:2] == [0.0, 1.0]
    card = {x["target"]: x for x in evc.get("/v1/scorecard", params={"suite": "refund"}).json()["items"]}
    # 스코어카드는 타깃별 최신 실행 — prompt-v2 의 최신은 미보정 judge(길이) 실행
    assert card["prompt-v2"]["run_id"] == u["run_id"] and card["prompt-v2"]["task_success_rate"] == u["summary"]["pass_rate"]
    assert card["prompt-v1"]["task_success_rate"] == 0.0 and card["prompt-v2"]["cost_per_task_usd"] > 0


def test_online_sampling_candidates_and_feedback(evc):
    evc.post("/v1/judges", json={"name": "online", "rubric_md": RUBRIC})
    rule = evc.post("/v1/online-rules", json={"name": "refund-prod", "filter": 'service = "atlas-agent-al-run"', "sample_rate": 1.0, "judge": "online"}).json()
    out = evc.post(f"/v1/online-rules/{rule['id']}/run").json()
    assert (out["fetched"], out["scored"], out["failed"], len(out["candidates"])) == (3, 3, 2, 2)
    again = evc.post(f"/v1/online-rules/{rule['id']}/run").json()
    assert again["scored"] == 0                                         # 이미 채점한 trace 는 건너뜀
    cands = evc.get("/v1/datasets/online-candidates/items").json()["items"]
    assert {c["source_trace_id"] for c in cands} == {"t-bad", "t-bad2"} and all(c["status"] == "in_review" for c in cands)
    fb = evc.post("/v1/feedback", json={"trace_id": "t-fb", "value": "down", "comment": "틀림"}).json()
    up = evc.post("/v1/feedback", json={"trace_id": "t-good", "value": "up"}).json()
    assert (fb["negative"], bool(fb["candidate_item"]), up["negative"], up["candidate_item"]) == (True, True, False, None)
    assert evc.post("/v1/feedback", json={"trace_id": "x", "value": "meh"}).status_code == 422
    assert len(evc.get("/v1/scores", params={"trace_id": "t-fb"}).json()["items"]) == 1


def test_stats():
    assert stats.cohen_kappa([True, True, False, False], [True, False, False, False]) == 0.5
    assert stats.mcnemar(0, 0) == 1.0 and stats.mcnemar(10, 0) < 0.01
    lo, hi = stats.bootstrap_diff_ci([0, 1, 0, 1, 0, 1, 0, 1], [1, 1, 1, 1, 0, 1, 1, 1])
    assert lo <= 0.375 <= hi
    flat = [0.9] * 20 + [0.9, 0.88, 0.5, 0.4, 0.3, 0.3]
    d = stats.ewma_drift(flat, baseline=20)
    assert d["drift"] and d["direction"] == "down" and not stats.ewma_drift([0.9] * 30, baseline=20)["drift"]
