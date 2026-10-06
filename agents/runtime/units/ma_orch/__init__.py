"""MA-ORCH Orchestrator — 목표 분해 → 위임 명세 → 병렬 fan-out(A2A) → fan-in 배리어 → MA-MERGE 병합·검증
(FR-MA-ORCH-01·02·03·04·05·07·09·10). 서브에이전트는 skill 레지스트리로 찾고 서명 카드를 검증한 뒤에만 위임한다."""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from atlas_agent_core.a2a_client import DepthExceeded
from units.ma_common import client_for, depth_of, estimate_tokens

MANIFEST = Path(__file__).with_name("atlas-agent.json")

KIND_SKILL = {"research": "ma.research", "review": "ma.code_review", "analysis": "ma.analysis"}
SCHEMA = {"research": "ResearchResult", "review": "ReviewResult", "analysis": "AnalysisResult"}
KEYWORDS = [("review", r"리뷰|검토|review|diff|코드"), ("analysis", r"분석|통계|집계|차트|매출|analy|data|데이터"),
            ("research", r"조사|찾|문서|자료|research|설명|왜")]


def classify(goal: str, inputs: dict[str, Any], effort: str | None) -> tuple[str, int]:
    """ORCH-03: 단순 → 1, 비교 → 2~4, 복잡 → 5~10 (상한 maxSubagents)."""
    if effort in ("simple", "compare", "complex"):
        level = effort
    else:
        kinds = sum(1 for k in ("diff", "data") if inputs.get(k)) + (1 if re.search(KEYWORDS[2][1], goal, re.IGNORECASE) else 0)
        level = "complex" if kinds >= 3 or len(goal) > 400 else "compare" if kinds == 2 or re.search(r"비교|vs|compare|각각", goal, re.IGNORECASE) else "simple"
    return level, {"simple": 1, "compare": 4, "complex": 10}[level]


def kind_of(text: str, inputs: dict[str, Any]) -> str:
    for kind, pat in KEYWORDS:
        if re.search(pat, text, re.IGNORECASE):
            if kind == "review" and not inputs.get("diff"):
                continue
            if kind == "analysis" and not inputs.get("data"):
                continue
            return kind
    return "research"


def density(steps: list[dict[str, Any]]) -> float:
    n = len(steps)
    return 0.0 if n < 2 else sum(len(s.get("depends_on", [])) for s in steps) / (n * (n - 1) / 2)


async def orchestrate(ctx: TaskContext) -> dict[str, Any]:
    goal = (ctx.data.get("goal") or ctx.text).strip()
    if not goal:
        raise InputRequired("목표(goal)가 필요합니다")
    cfg = agent.manifest["spec"]["config"]
    inputs = ctx.data.get("inputs") or {}
    join = ctx.data.get("join", "all")                     # all · quorum · first · deadline
    deadline_s = float(ctx.data.get("deadline_s") or cfg["defaultDeadlineS"])
    budget = int(ctx.data.get("budget_tokens") or cfg["budgetTokens"])
    depth = depth_of(ctx.history)
    cli = client_for(agent, ctx.tenant)
    if depth >= cli.max_depth:   # ORCH-10: 서브가 또 오케스트레이터를 부르는 무한 중첩 차단
        raise PermissionError(f"위임 깊이 {depth} ≥ max_depth {cli.max_depth} — 더 이상 서브에이전트를 만들 수 없음")

    # ① 분해 (ORCH-01): AL-PLAN(A2A) 재사용, 의존 밀도가 높으면 단일 에이전트 모드로 강등
    level, cap = classify(goal, inputs, ctx.data.get("effort"))
    cap = min(cap, int(ctx.data.get("max_subagents") or cfg["maxSubagents"]))
    plan = await cli.delegate({"goal": goal}, skill="loop.plan", depth=depth, parent=ctx.task_id, timeout=30)
    steps = (plan.get("output") or {}).get("steps") or [{"title": goal, "depends_on": []}]
    dens = density(steps)
    mode = "single" if dens > float(cfg["maxDependencyDensity"]) or level == "simple" else "multi"
    titles = [goal] if mode == "single" else [s["title"] for s in steps]
    kinds = [kind_of(t, inputs) for t in titles]
    if mode == "multi" and len(set(kinds)) == 1:
        # 계획이 관점을 나누지 못했으면(쉼표 나열 등) 목표에 드러난 관점마다 과업을 만든다
        seen = [k for k, pat in KEYWORDS if re.search(pat, goal, re.IGNORECASE) and (k != "review" or inputs.get("diff")) and (k != "analysis" or inputs.get("data"))]
        if len(seen) > 1:
            titles, kinds = [f"{goal} — {k}" for k in seen], seen
    # 입력이 있는 관점(diff·data)은 빠지지 않게 과업을 보장한다
    for need, key in (("review", "diff"), ("analysis", "data")):
        if inputs.get(key) and need not in kinds and mode == "multi":
            titles.append(f"{goal} — {need}")
            kinds.append(need)
    pairs = list(zip(titles, kinds))[: max(1, cap)]
    if len(pairs) > int(cfg["maxTotalSpawns"]):
        pairs = pairs[: int(cfg["maxTotalSpawns"])]

    # ② 위임 명세 (ORCH-02) + 조정 비용 분할 (ORCH-07)
    worker_budget = int(budget * (1 - float(cfg["coordinationShare"])) / max(1, len(pairs)))
    specs = []
    for title, kind in pairs:
        specs.append({"task_id": f"t{len(specs) + 1}", "objective": title, "output_schema_ref": SCHEMA[kind], "kind": kind,
                      "tools_allow": [], "sources_hint": ["atlas://corpus"] if kind == "research" else [],
                      "boundaries": {"do": ["읽기 전용 조사·검토·계산"], "dont": ["쓰기·배포·외부 네트워크", "다른 서브에이전트 생성"]},
                      "budget": {"tokens": worker_budget}, "deadline_s": deadline_s, "context_mode": "isolated"})

    # ③ 병렬 fan-out (ORCH-04) — 동시성 제한, 깊이 전파 (ORCH-10)
    sem = asyncio.Semaphore(int(cfg["concurrency"]))

    async def run(spec: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            msg = {"spec": spec, "query": spec["objective"], **{k: v for k, v in inputs.items() if k in ("diff", "data", "code")}}
            try:
                r = await cli.delegate(msg, skill=KIND_SKILL[spec["kind"]], depth=depth, parent=ctx.task_id, timeout=deadline_s)
            except DepthExceeded as exc:
                r = {"state": "TASK_STATE_REJECTED", "reason": "depth_exceeded", "message": str(exc), "agent": None, "output": None}
            return {**r, "spec_task_id": spec["task_id"]}

    jobs = [asyncio.create_task(run(s)) for s in specs]
    need = {"all": len(jobs), "first": 1, "quorum": int(ctx.data.get("quorum") or max(1, len(jobs) // 2 + 1)), "deadline": len(jobs)}[join]
    done_results: list[dict[str, Any]] = []
    # ④ fan-in 배리어 (ORCH-05): 정족수·마감에 도달하면 남은 작업은 취소하고 부분 결과로 진행
    try:
        for fut in asyncio.as_completed(jobs, timeout=deadline_s + 5):
            done_results.append(await fut)
            if len([r for r in done_results if r.get("state") == "TASK_STATE_COMPLETED"]) >= need and join != "all":
                break
    except TimeoutError:
        pass
    for j in jobs:
        if not j.done():
            j.cancel()
    finished = {r["spec_task_id"] for r in done_results}
    for s in specs:
        if s["task_id"] not in finished:
            done_results.append({"spec_task_id": s["task_id"], "state": "TASK_STATE_CANCELED", "reason": f"join:{join}", "agent": None, "output": None})
    partial = any(r.get("state") != "TASK_STATE_COMPLETED" for r in done_results)

    # ⑤ 병합·검증 (MA-MERGE, MERGE-08)
    merged = await cli.delegate({"goal": goal, "results": done_results, "strategy": ctx.data.get("strategy", "auto"), "dod": ctx.data.get("dod") or []},
                                skill="ma.merge", depth=depth, parent=ctx.task_id, timeout=60)
    m = merged.get("output") or {}
    worker_tokens = sum(int(((r.get("output") or {}).get("usage") or {}).get("tokens", 0)) for r in done_results)
    orch_tokens = estimate_tokens(goal + str(steps)) + int((m.get("usage") or {}).get("tokens", 0))
    ratio = round(orch_tokens / max(1, orch_tokens + worker_tokens), 3)
    return {
        "run_id": str(uuid.uuid4()), "goal": goal, "mode": mode, "complexity": level, "dependency_density": round(dens, 3), "join": join,
        "subtasks": [{"spec": s, **next(({k: r.get(k) for k in ("state", "reason", "taskId", "elapsed_s")} | {"agent": (r.get("agent") or {}).get("name")}
                                          for r in done_results if r["spec_task_id"] == s["task_id"]), {})} for s in specs],
        "partial": partial, "merged": {k: m.get(k) for k in ("strategy", "report", "issues", "answer", "verified", "gaps", "sections", "deduplicated")},
        "metrics": {"subagents": len(specs), "spawns": len(specs) + 2, "depth": depth, "worker_tokens": worker_tokens, "coordination_tokens": orch_tokens,
                    "coordination_overhead_ratio": ratio, "overhead_alert": ratio > float(cfg["overheadAlert"])},
        "writes": "orchestrator-only",   # ORCH-09: 서브에이전트는 읽기 전용(tools_allow 비움), 결과 반영은 여기서만
    }


agent = AgentSpec.from_file(MANIFEST, description="MA-ORCH 오케스트레이터 — 분해·위임 명세·병렬 위임·배리어·병합(다중 에이전트)",
                            skills=[Skill(id="ma.orchestrate", name="Orchestrate", handler=orchestrate, security="required",
                                          description="goal(+inputs{diff,data}) → 서브에이전트(조사·리뷰·분석) 병렬 위임 → 병합 보고서 orchestrate 오케스트레이션",
                                          tags=["orchestrator", "multi-agent"],
                                          examples=["이 변경을 보안 관점에서 리뷰하고 관련 설계 원칙을 조사해 함께 정리"])])
