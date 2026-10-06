"""Epic 17 다중 에이전트 단위 — 조사·리뷰·병합·오케스트레이터 규칙 (분석·샌드박스·실제 위임은 스택 시험 E17)."""
import asyncio

import pytest

from atlas_agent_core.spec import TaskContext
from units import ma_cr, ma_merge, ma_orch, ma_res
from units.ma_common import check_spec, depth_of

DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,2 +10,5 @@ def q():
 x = 1
+API_KEY = "sk-live-1234567890"
+cur.execute("SELECT * FROM t WHERE id=" + uid)
+print(x)
"""


def ctx(data, text="", history=None):
    return TaskContext(tenant="t", task_id="1", context_id="c", skill="x", text=text, data=data, history=history or [])


def test_research_requires_sources_and_is_read_only():
    out = asyncio.run(ma_res.research(ctx({"query": "다중 에이전트 조정 비용 병목"})))
    ids = {s["id"] for s in out["sources"]}
    assert out["findings"] and all(f["source_ids"] and set(f["source_ids"]) <= ids for f in out["findings"])
    assert out["findings"][0]["source_ids"] == ["multi-agent#p2"] and out["read_only"]


def test_code_review_issues_sarif_and_write_tools_rejected():
    out = asyncio.run(ma_cr.code_review(ctx({"diff": DIFF})))
    rules = [(i["rule"], i["line_start"], i["severity"]) for i in out["issues"]]
    assert ("hardcoded-secret", 11, "critical") in rules and ("sql-concat", 12, "high") in rules and ("debug-print", 13, "low") in rules
    assert out["sarif"]["version"] == "2.1.0" and len(out["sarif"]["runs"][0]["results"]) == len(out["issues"])
    with pytest.raises(PermissionError):
        check_spec({"spec": {"tools_allow": ["repo.write_file"]}})


def results(*outs):
    return [{"taskId": f"t{i}", "agent": {"name": o.get("kind")}, "state": "TASK_STATE_COMPLETED", "output": o} for i, o in enumerate(outs)]


def test_merge_structural_dedupes_and_keeps_max_severity():
    a = {"kind": "review", "issues": [{"file": "a.py", "line_start": 3, "category": "security", "severity": "medium", "rule": "x"}]}
    b = {"kind": "review", "issues": [{"file": "a.py", "line_start": 3, "category": "security", "severity": "critical", "rule": "x"},
                                      {"file": "b.py", "line_start": 1, "category": "style", "severity": "low", "rule": "y"}]}
    out = asyncio.run(ma_merge.merge(ctx({"results": results(a, b)})))
    assert out["strategy"] == "structural" and out["deduplicated"] == 1 and out["issues"][0]["severity"] == "critical"
    assert out["issues"][0]["sources"] == ["t0", "t1"]


def test_merge_synthesis_keeps_citations_vote_and_gaps():
    r = asyncio.run(ma_res.research(ctx({"query": "prompt cache 비용"})))
    c = asyncio.run(ma_cr.code_review(ctx({"diff": DIFF})))
    res = results(r, c) + [{"taskId": "t9", "state": "TASK_STATE_CANCELED", "reason": "timeout", "output": None}]
    out = asyncio.run(ma_merge.merge(ctx({"goal": "정리", "results": res, "dod": [{"type": "test", "spec": "contains:hardcoded-secret"},
                                                                               {"type": "test", "spec": "contains:없는문자열"}]})))
    assert out["strategy"] == "synthesis" and "[economics#p" in out["report"] and "hardcoded-secret" in out["report"]
    assert not out["verified"] and {g.get("task_id") for g in out["gaps"]} >= {"t9"} and any("dod" in g for g in out["gaps"])
    v = asyncio.run(ma_merge.merge(ctx({"results": results({"answer": 42}, {"answer": 42}, {"answer": 7})})))
    assert v["strategy"] == "vote" and v["answer"] == 42 and v["votes"] == 2


def test_orchestrator_rules():
    assert ma_orch.classify("설명해줘", {}, None) == ("simple", 1)
    assert ma_orch.classify("A 와 B 를 비교", {}, None)[0] == "compare"
    assert ma_orch.classify("리뷰하고 분석하고 조사해", {"diff": "x", "data": "y"}, None)[0] == "complex"
    assert ma_orch.density([{"depends_on": []}, {"depends_on": ["s1"]}, {"depends_on": ["s1", "s2"]}]) == 1.0
    assert ma_orch.kind_of("이 diff 를 리뷰", {"diff": "x"}) == "review" and ma_orch.kind_of("이 diff 를 리뷰", {}) == "research"
    assert depth_of([{"metadata": {"x-atlas-depth": 1}}]) == 1 and depth_of([]) == 0
    card_skill = next(s for s in ma_orch.agent.skills if s.id == "ma.orchestrate")
    assert card_skill.security == "required"
