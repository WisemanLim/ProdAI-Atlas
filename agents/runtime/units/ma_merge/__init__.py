"""MA-MERGE Merged Result — 서브 결과를 정규화하고 전략(synthesis·structural·vote)으로 합친 뒤 DoD 로 검증한다
(FR-MA-MERGE-01·02·03·06·08)."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from units import al_ref
from units.ma_common import estimate_tokens, llm

MANIFEST = Path(__file__).with_name("atlas-agent.json")
SEV = ["critical", "high", "medium", "low", "info"]


def normalize(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MERGE-01: {task_id, kind, ok, content, error} — 실패·부분 결과도 버리지 않고 표시한다."""
    out = []
    for r in results:
        content = r.get("output") if isinstance(r.get("output"), dict) else {}
        out.append({"task_id": r.get("taskId") or r.get("task_id"), "agent": (r.get("agent") or {}).get("name"),
                    "kind": content.get("kind") or r.get("kind") or "unknown", "ok": r.get("state") == "TASK_STATE_COMPLETED",
                    "content": content, "error": None if r.get("state") == "TASK_STATE_COMPLETED" else f"{r.get('state')}:{r.get('reason')}"})
    return out


def choose(items: list[dict[str, Any]], strategy: str) -> str:
    """MERGE-02 auto: 이질 과업 → synthesis, 동일 과업 목록형(issues) → structural, 동일 과업 답 N개 → vote."""
    if strategy != "auto":
        return strategy
    kinds = {i["kind"] for i in items if i["ok"]}
    if len(kinds) > 1:
        return "synthesis"
    if kinds == {"review"}:
        return "structural"
    if len([i for i in items if i["ok"]]) > 1 and all("answer" in i["content"] for i in items if i["ok"]):
        return "vote"
    return "synthesis"


def structural(items: list[dict[str, Any]]) -> dict[str, Any]:
    """MERGE-06: 키(file+line+category) 기준 중복 제거, 심각도는 높은 쪽, 출처 task 를 모은다."""
    merged: dict[tuple, dict[str, Any]] = {}
    for it in items:
        for iss in it["content"].get("issues", []) if it["ok"] else []:
            k = (iss.get("file"), iss.get("line_start"), iss.get("category"))
            cur = merged.get(k)
            if cur is None:
                merged[k] = {**iss, "sources": [it["task_id"]]}
            else:
                if SEV.index(iss.get("severity", "info")) < SEV.index(cur.get("severity", "info")):
                    cur.update(severity=iss["severity"], rationale=iss.get("rationale"))
                cur["sources"].append(it["task_id"])
    issues = sorted(merged.values(), key=lambda i: (SEV.index(i.get("severity", "info")), str(i.get("file")), i.get("line_start") or 0))
    return {"issues": issues, "deduplicated": sum(len(i["content"].get("issues", [])) for i in items if i["ok"]) - len(issues)}


def vote(items: list[dict[str, Any]]) -> dict[str, Any]:
    canon = [json.dumps(i["content"]["answer"], sort_keys=True, ensure_ascii=False) for i in items if i["ok"]]
    winner, n = Counter(canon).most_common(1)[0]
    return {"answer": json.loads(winner), "votes": n, "of": len(canon)}


def section(it: dict[str, Any]) -> str:
    c = it["content"]
    if not it["ok"]:
        return f"## {it['agent']} ({it['kind']}) — 실패: {it['error']}"
    if it["kind"] == "research":
        cites = "; ".join(f"{f['claim']} [{', '.join(f['source_ids'])}]" for f in c.get("findings", [])[:5])
        return f"## 조사 ({it['agent']})\n{cites}"
    if it["kind"] == "review":
        return f"## 코드 리뷰 ({it['agent']})\n{c.get('summary', '')}\n" + "\n".join(
            f"- [{i['severity']}] {i['file']}:{i['line_start']} {i['rule']} — {i['rationale']}" for i in c.get("issues", [])[:20])
    if it["kind"] == "analysis":
        ms = ", ".join(f"{m['name']}={m['value']}" for m in c.get("metrics", [])[:10])
        return f"## 분석 ({it['agent']})\n{c.get('interpretation', '')}\n{ms}"
    return f"## {it['agent']}\n{json.dumps(c, ensure_ascii=False)[:1000]}"


async def merge(ctx: TaskContext) -> dict[str, Any]:
    results = ctx.data.get("results")
    if not isinstance(results, list) or not results:
        raise InputRequired("병합할 results[] 가 필요합니다")
    goal, dod = ctx.data.get("goal", ""), ctx.data.get("dod") or []
    items = normalize(results)
    strategy = choose(items, ctx.data.get("strategy", "auto"))
    out: dict[str, Any] = {"strategy": strategy, "inputs": len(items), "failed": [i["task_id"] for i in items if not i["ok"]]}
    used = 0
    if strategy == "structural":
        out |= structural(items)
        report = "\n".join(f"- [{i['severity']}] {i['file']}:{i['line_start']} {i.get('rule', '')}" for i in out["issues"])
    elif strategy == "vote":
        out |= vote(items)
        report = json.dumps(out["answer"], ensure_ascii=False)
    else:  # synthesis (MERGE-03): 섹션=과업, 주장마다 출처 id 유지, 원문 대신 요약만 입력, 길이 상한
        sections = [section(i) for i in items]
        draft = f"# {goal}\n\n" + "\n\n".join(sections)
        content, used = await llm([{"role": "system", "content": "섹션별 결과를 하나의 보고서로 합성하라. 출처 [id] 를 지우지 마라."},
                                   {"role": "user", "content": draft[:8000]}], f"agent_id:ma-merge,unit_id:MA-MERGE,tenant:{ctx.tenant}")
        report = draft if not content or content.startswith("[mock") else content
        out["sections"] = len(sections)
    report = report[: int(agent.manifest["spec"]["config"]["maxReportChars"])]
    # 병합 후 검증 (MERGE-08): DoD 를 AL-REF 로 검사, 실패하면 gaps[] 로 오케스트레이터에 알린다
    checks = [al_ref.check_dod(d, report) for d in dod]
    gaps = [{"dod": d, "reason": c.reason} for d, c in zip(dod, checks) if c.checkable and not c.passed]
    gaps += [{"task_id": t, "reason": "서브 과업 실패 — 부분 결과로 병합"} for t in out["failed"]]
    out |= {"report": report, "verified": not gaps and bool(dod), "gaps": gaps, "usage": {"tokens": used or estimate_tokens(report)}}
    return out


agent = AgentSpec.from_file(MANIFEST, description="MA-MERGE 결과 병합 — 정규화·전략 선택(synthesis·structural·vote)·출처 유지 합성·DoD 검증·갭 반환",
                            skills=[Skill(id="ma.merge", name="Merge", handler=merge,
                                          description="results[] + strategy + dod → 병합 보고서·gaps merge 병합 synthesis",
                                          tags=["merge", "병합"])])
