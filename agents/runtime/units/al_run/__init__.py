"""AL-RUN Loop Runtime — 'The loop turns an LLM call into an agent.' (FR-AL-RUN, 단위 AL-ACT·OBS·REF·EXIT 를 합성)

A2A skill loop.run(goal, dod?, budget?, tools?, permission_mode?) · loop.resume(run_id, checkpoint?, approval?).
HITL 승인은 같은 Task 에 data {"approval": "approve"|"deny"|"abort", "edited_arguments"?} 를 보내 재개한다.
승인 대기에 들어가면 Manager 통합 승인함(POST /api/v1/approvals)에 요청을 올린다 — 승인자의 결정이 같은 Task 로 돌아온다 (FR-X-HITL-01).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from opentelemetry import trace

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext, prompts
from atlas_agent_core.a2a_client import TokenProvider
from units import al_exit, mcp_sel
from units.al_act import BUILTINS, ActConfig
from units.al_run import clients
from units.al_run.engine import CompactConfig, LoopEngine, Outcome
from units.al_run.store import LoopStore
from units.mcp_host import McpHost

MANIFEST = Path(__file__).with_name("atlas-agent.json")
TERMINAL = {"success", "truncated", "aborted", "blocked", "failed"}

# 테스트·확장에서 교체할 수 있는 구성 요소
EXTRA_TOOLS: list = []          # MCP 도구 등 (Epic 7)
POLICY_HOOK = None              # GR-POL 도구 인가 훅 (Epic 8)


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, d))


def build_engine(budget: dict[str, Any] | None = None, permission_mode: str | None = None, tenant: str = "default") -> LoopEngine:
    budget = budget or {}
    cap = _env_int("ATLAS_AL_EXIT_MAX_TURNS_CAP", 100)   # 테넌트 상한
    exit_cfg = al_exit.ExitConfig(
        max_turns=min(int(budget.get("max_turns", _env_int("ATLAS_AL_EXIT_MAX_TURNS", 25))), cap),
        max_llm_calls=min(int(budget.get("max_llm_calls", 50)), cap * 2),
        max_tokens=int(budget.get("max_tokens", 0)), max_usd=float(budget.get("max_usd", 0)),
        max_input_tokens=int(budget.get("max_input_tokens", 0)), max_output_tokens=int(budget.get("max_output_tokens", 0)),
        require_external_verify=bool(budget.get("require_external_verify", os.environ.get("ATLAS_AL_REF_REQUIRE_EXTERNAL_VERIFY", "false") == "true")),
        timeout_s=float(budget.get("timeout_s", 600)),
        end_turn_policy=os.environ.get("ATLAS_AL_EXIT_END_TURN_POLICY", "verify_then_accept"))
    mode = permission_mode or os.environ.get("ATLAS_AL_ACT_PERMISSION_MODE", "default")
    if mode == "bypass" and os.environ.get("ATLAS_AL_ACT_ALLOW_BYPASS") != "true":
        mode = "default"
    act_cfg = ActConfig(permission_mode=mode, max_tokens_per_result=_env_int("ATLAS_AL_OBS_MAX_TOKENS", 8000),
                        backoff_base_s=float(os.environ.get("ATLAS_AL_ACT_BACKOFF_S", "0.5")))
    compact = CompactConfig(context_window=_env_int("ATLAS_AL_RUN_CONTEXT_WINDOW", 128_000))
    gateway, planner = clients.from_env()
    tools = {t.name: t for t in [*BUILTINS, *EXTRA_TOOLS]}
    guard = None
    if url := os.environ.get("ATLAS_GUARD_URL"):
        guard = clients.GuardClient(url, tenant, on_error=os.environ.get("ATLAS_GUARD_ON_ERROR", "fail_open"))
    return LoopEngine(LoopStore(agent.resources["engine"]), gateway, planner, tools,
                      exit_cfg=exit_cfg, act_cfg=act_cfg, compact=compact,
                      policy=POLICY_HOOK or (guard.tool_policy if guard else None), guard=guard)


_HOSTS: dict[str, McpHost] = {}


async def attach_mcp(engine: LoopEngine, tenant: str, goal: str | None) -> tuple[list[str] | None, list[tuple[str, dict]]]:
    """MCP-REG 허용 도구를 엔진에 붙이고, goal 이 있으면 MCP-SEL 로 초기 노출 도구를 고른다."""
    url = os.environ.get("ATLAS_MCP_REGISTRY_URL")
    if not url:
        return None, []
    host = _HOSTS.setdefault(tenant, McpHost(url, tenant))
    try:
        specs, report = await host.tools()
    except Exception as exc:  # noqa: BLE001 — 레지스트리 장애 시 내장 도구로 계속 (fail-static)
        return None, [("mcp", {"error": f"{type(exc).__name__}: {exc}"[:200]})]
    for s in [*specs, *mcp_sel.meta_tools(specs)]:
        engine.tools[s.name] = s
    events = [("mcp", {"servers": report.servers, "blocked": report.blocked, "tools": len(specs)})]
    if goal is None:
        return None, events
    k = _env_int("ATLAS_MCP_SEL_TOPK", 5)
    picked = mcp_sel.select(goal, specs, k=k, budget_tokens=_env_int("ATLAS_MCP_SEL_MAX_TOOL_TOKENS", 8000))
    names = [t.name for t in BUILTINS] + picked + (["search_tools", "load_tools"] if len(specs) > len(picked) else [])
    events.append(("tools_selected", {"selected": picked, "catalog": len(specs)}))
    return names, events


_http = httpx.AsyncClient      # Manager 호출 클라이언트 (시험에서 교체)
_requested: set[tuple[str, str, tuple[str, ...]]] = set()   # (tenant, task, call ids) — 같은 대기를 두 번 올리지 않는다


def _manager() -> tuple[str, dict[str, Any]]:
    return agent.resources.get("manager_url") or os.environ.get("ATLAS_MANAGER_URL", ""), agent.resources.get("registration") or {}


async def _mgr_post(path: str, tenant: str, body: dict[str, Any]) -> dict[str, Any] | None:
    mgr, reg = _manager()
    if not mgr or not reg.get("agent_id"):
        return None
    try:
        tok = await TokenProvider(mgr, os.environ.get("ATLAS_MANAGER_TOKEN"), reg)()
        async with _http(timeout=5.0) as c:
            res = await c.post(f"{mgr.rstrip('/')}{path}", json=body, params={"org": tenant}, headers={"Authorization": f"Bearer {tok}"} if tok else {})
        res.raise_for_status()
        return res.json()
    except (httpx.HTTPError, ValueError):
        return None


async def withdraw_approvals(ctx: TaskContext) -> None:
    """같은 Task 로 직접 응답이 와서 재개되면 승인함의 대기 요청을 거둔다(승인함 결정이면 이미 pending 아님 → no-op)."""
    _, reg = _manager()
    if reg.get("agent_id"):
        await _mgr_post("/api/v1/approvals/withdraw", ctx.tenant, {"sourceRef": ctx.task_id, "agentId": reg["agent_id"]})


def action_preview(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """승인 화면 실제 행위 (FR-GR-POL-06): 사람이 읽을 요약 + 구조화 인자 (LLM 설명이 아니라 실행될 호출 그대로)."""
    def args(c: dict[str, Any]) -> dict[str, Any]:
        a = c.get("arguments")
        if isinstance(a, str):
            try:
                a = json.loads(a or "{}")
            except json.JSONDecodeError:
                return {"_raw": a}
        return a if isinstance(a, dict) else {}
    summary = "; ".join(f"{c['tool']}(" + ", ".join(f"{k}={v!r}" for k, v in args(c).items())[:200] + ")" for c in calls)
    return {"human_summary": summary, "structured_args": [{"tool": c["tool"], "arguments": args(c)} for c in calls]}


async def request_approval(ctx: TaskContext, outcome: Outcome) -> str | None:
    """승인 대기를 Manager 승인함에 올린다(실패해도 A2A INPUT_REQUIRED 경로는 그대로)."""
    calls = outcome.result.get("pending_approval", [])
    mgr, reg = _manager()
    key = (ctx.tenant, ctx.task_id, tuple(c["id"] for c in calls))
    if not calls or not mgr or not reg.get("agent_id") or key in _requested:
        return None
    span = trace.get_current_span().get_span_context()
    body = {"source": "loop", "sourceRef": ctx.task_id, "agentId": reg["agent_id"], "unitId": "AL-RUN",
            "action": ", ".join(c["tool"] for c in calls), "riskLevel": "high",
            "payload": {"run_id": outcome.result.get("run_id"), "calls": calls, "reason": outcome.decision.detail,
                        "arguments": calls[0].get("arguments") if len(calls) == 1 else None,
                        # 승인 화면 실제 행위 (FR-GR-POL-06): 사람이 읽을 요약 + 구조화 인자 (LLM 설명 아님)
                        "action_preview": action_preview(calls)},
            "traceId": format(span.trace_id, "032x") if span.trace_id else "",
            "expiresInS": int(al_exit.ExitConfig().hitl_timeout_s)}
    out = await _mgr_post("/api/v1/approvals", ctx.tenant, body)
    if out:
        _requested.add(key)
    return (out or {}).get("id")


async def _settle_async(ctx: TaskContext, outcome: Outcome) -> dict[str, Any]:
    if outcome.decision.reason == al_exit.HITL_PENDING and (aid := await request_approval(ctx, outcome)):
        outcome.result["approval_id"] = aid
    return _settle(outcome)


def _settle(outcome: Outcome) -> dict[str, Any]:
    if outcome.decision.reason == al_exit.HITL_PENDING:
        raise InputRequired("승인 필요 — data {\"approval\": \"approve\"|\"deny\"|\"abort\"} 로 응답: "
                            + json.dumps(outcome.result.get("pending_approval", []), ensure_ascii=False))
    if outcome.decision.reason == al_exit.ERROR:
        raise RuntimeError(outcome.decision.detail)
    return outcome.result


async def run(ctx: TaskContext) -> dict[str, Any]:
    store = LoopStore(agent.resources["engine"])
    if store.get_run(ctx.tenant, ctx.task_id):           # 같은 Task 이어가기 = HITL 응답
        approval = ctx.data.get("approval") or ctx.text.strip().lower() or None
        engine = build_engine(tenant=ctx.tenant)
        await attach_mcp(engine, ctx.tenant, None)
        if approval:
            await withdraw_approvals(ctx)
        return await _settle_async(ctx, await engine.resume(ctx.tenant, ctx.task_id, decision=approval,
                                                            edited_arguments=ctx.data.get("edited_arguments")))
    goal = (ctx.data.get("goal") or ctx.text).strip()
    if not goal:
        raise InputRequired("실행할 목표(goal)를 text 또는 data.goal 로 보내세요")
    h = await prompts.harness("al-run")   # 하네스 매니페스트 (BL-HARN-03): 코드 배포 없이 루프 설정 변경
    hb = {"max_turns": h["loop"]["max_turns"]} if (h.get("loop") or {}).get("max_turns") else {}
    engine = build_engine({**hb, **(ctx.data.get("budget") or {})}, ctx.data.get("permission_mode"), tenant=ctx.tenant)
    names, events = await attach_mcp(engine, ctx.tenant, goal)
    if h:
        events = [*events, ("harness", {"max_turns": hb.get("max_turns"), "tools": h.get("tools")})]
        if h.get("tools"):
            names = [n for n in (names or [t.name for t in BUILTINS]) if n in set(h["tools"]) or n in ("search_tools", "load_tools")]
    return await _settle_async(ctx, await engine.start(ctx.tenant, ctx.task_id, goal, dod=ctx.data.get("dod"),
                                                       tool_names=ctx.data.get("tools") or names, pre_events=events))


async def resume(ctx: TaskContext) -> dict[str, Any]:
    run_id = ctx.data.get("run_id")
    if not run_id:
        raise InputRequired("run_id 가 필요합니다")
    engine = build_engine(tenant=ctx.tenant)
    await attach_mcp(engine, ctx.tenant, None)
    try:
        return _settle(await engine.resume(ctx.tenant, run_id, decision=ctx.data.get("approval"),
                                           checkpoint=ctx.data.get("checkpoint")))
    except KeyError as exc:
        raise ValueError(f"run {run_id} 체크포인트 없음") from exc


router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def list_runs(tenant: str = Query("default"), status: str | None = None, limit: int = Query(50, ge=1, le=200)):
    """런 목록 (콘솔 루프 화면). status: running | pending | success | truncated | aborted | blocked | failed."""
    rows = LoopStore(agent.resources["engine"]).list_runs(tenant, limit, status)
    return {"items": [{"id": r.id, "goal": r.goal[:200], "status": r.status, "reason": r.reason, "turns": r.turns,
                       "updated_at": r.updated_at.isoformat()} for r in rows]}


@router.get("/{run_id}")
async def get_run(run_id: str, tenant: str = Query("default")):
    row = LoopStore(agent.resources["engine"]).get_run(tenant, run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    return {"id": row.id, "tenant": row.tenant, "goal": row.goal, "status": row.status, "reason": row.reason,
            "turns": row.turns, "result": row.result}


@router.get("/{run_id}/artifacts/{key}", response_class=PlainTextResponse)
async def get_artifact(run_id: str, key: str):
    content = LoopStore(agent.resources["engine"]).get_artifact(run_id, key)
    if content is None:
        raise HTTPException(404, "artifact not found")
    return content


@router.get("/{run_id}/events")
async def events(run_id: str, tenant: str = Query("default"), follow: bool = True, max_wait_s: float = 300):
    """런 이벤트 SSE (FR-AL-RUN-08): 저장된 이벤트를 재생하고, follow 면 종료(exit)까지 이어서 흘린다."""
    store = LoopStore(agent.resources["engine"])
    if store.get_run(tenant, run_id) is None:
        raise HTTPException(404, "run not found")

    async def gen():
        last, waited = 0, 0.0
        while True:
            batch = store.events(run_id, last)
            for e in batch:
                last = e.seq
                yield f"id: {e.seq}\nevent: {e.type}\ndata: {json.dumps(e.data, ensure_ascii=False)}\n\n"
            run_row = store.get_run(tenant, run_id)
            done = run_row.status in TERMINAL or run_row.status == "pending"
            if not follow or (done and not batch) or waited >= max_wait_s:
                return
            await asyncio.sleep(0.5)
            waited += 0.5

    return StreamingResponse(gen(), media_type="text/event-stream")


agent = AgentSpec.from_file(
    MANIFEST,
    description="AL-RUN Loop Runtime — Plan→Act→Observe→Reflect 루프를 종료 조건까지 구동한다(체크포인트·HITL·반복 탐지).",
    skills=[Skill(id="loop.run", name="Run", handler=run, description="목표를 루프로 실행해 RunResult 반환 (FR-AL-RUN-01·EXIT-08)",
                  examples=['atlas_calc({"expr":"6*7"}) 로 계산해 답한다']),
            Skill(id="loop.resume", name="Resume", handler=resume, description="체크포인트에서 런 재개 (FR-AL-RUN-03)")],
    routers=[router],
)
