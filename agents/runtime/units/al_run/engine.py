"""AL-RUN 루프 엔진 — PLAN → ACT → OBSERVE → REFLECT → EXIT 상태기계 (FR-AL-RUN-01·02·03·05).

상태(state)는 매 단계 체크포인트로 저장돼 재시작·HITL 재개 시 그대로 이어진다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from opentelemetry import trace

from units import al_exit, al_obs, al_ref
from units.al_act import ActConfig, Actor, ApprovalNeeded, ToolSpec
from units.al_run.clients import (
    BudgetExceeded,
    GatewayClient,
    GatewayError,
    GuardBlocked,
    PlannerClient,
)
from units.al_run.store import LoopStore

SYSTEM = ("You are AL-RUN, the loop runtime of ProdAI Atlas. Work toward the goal with the available tools, "
          "one step at a time. When the definition of done is met, reply with the final answer and no tool calls.")


@dataclass
class CompactConfig:
    context_window: int = 128_000
    trigger_ratio: float = 0.8
    keep_last: int = 6


@dataclass
class Outcome:
    decision: al_exit.ExitDecision
    result: dict[str, Any]
    pending: list[dict[str, Any]] = field(default_factory=list)


def est_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(al_obs.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in messages)


class LoopEngine:
    def __init__(self, store: LoopStore, gateway: GatewayClient | None, planner: PlannerClient, tools: dict[str, ToolSpec], *,
                 exit_cfg: al_exit.ExitConfig | None = None, act_cfg: ActConfig | None = None,
                 compact: CompactConfig | None = None, policy=None, guard=None, clock=time.time):
        self.store, self.gateway, self.planner, self.tools, self.guard = store, gateway, planner, tools, guard
        self.exit_cfg, self.act_cfg = exit_cfg or al_exit.ExitConfig(), act_cfg or ActConfig()
        self.compact_cfg, self.policy, self.clock = compact or CompactConfig(), policy, clock

    # ---- 진입점 ----
    async def start(self, tenant: str, run_id: str, goal: str, dod: list[dict[str, Any]] | None = None,
                    tool_names: list[str] | None = None, pre_events: list[tuple[str, dict[str, Any]]] | None = None) -> Outcome:
        self.store.create_run(tenant, run_id, goal)
        try:
            plan = await self.planner.plan(tenant, goal)                                   # PLAN
        except GuardBlocked as exc:                                                         # 계획 단계에서 가드 차단 (FR-AL-EXIT-07)
            plan = {"id": None, "steps": [], "dod": []}
            state = {"run_id": run_id, "tenant": tenant, "goal": goal, "plan": plan, "dod": [], "turns": 0, "llm_calls": 0,
                     "usage": {"input": 0, "output": 0}, "cost_usd": "0", "messages": []}
            return self._finish(state, al_exit.ExitDecision(al_exit.POLICY_VIOLATION, str(exc)))
        state = {"run_id": run_id, "tenant": tenant, "goal": goal, "plan": plan, "dod": dod or plan.get("dod", []),
                 "tools": tool_names or sorted(self.tools), "turns": 0, "llm_calls": 0, "usage": {"input": 0, "output": 0},
                 "cost_usd": "0", "started_at": self.clock(), "recent_calls": [], "ctx": {}, "approved": [], "verify_failures": 0,
                 "messages": [{"role": "system", "content": self._system(plan, dod or plan.get("dod", []))},
                              {"role": "user", "content": goal}]}
        self._event(run_id, "plan", {"plan_id": plan.get("id"), "steps": len(plan.get("steps", [])), "source": plan.get("source")})
        for type_, data in pre_events or []:
            self._event(run_id, type_, data)
        self.store.checkpoint(tenant, run_id, "planning", state)
        return await self._loop(state)

    async def resume(self, tenant: str, run_id: str, decision: str | None = None, checkpoint: int | None = None,
                     edited_arguments: Any = None) -> Outcome:
        """마지막(또는 지정) 체크포인트에서 이어간다 (FR-AL-RUN-03). decision 은 HITL 응답 approve | deny | abort.
        edited_arguments = 승인자가 고친 인자 (수정 후 승인, FR-X-HITL-01): 대기 호출 1개면 dict, 여럿이면 {call_id: dict}."""
        cp = self.store.latest_checkpoint(tenant, run_id, checkpoint)
        if cp is None:
            raise KeyError(run_id)
        _, state = cp
        pending = state.get("pending")
        if pending:
            if decision in ("deny", "abort"):
                reason = al_exit.HITL_DENIED if decision == "deny" else al_exit.USER_ABORT
                state.pop("pending")
                trace.get_current_span().add_event("atlas.hitl.decision", {"decision": decision, "run_id": run_id})
                return self._finish(state, al_exit.ExitDecision(reason, f"사람 결정: {decision}"))
            if self.clock() - pending["since"] > self.exit_cfg.hitl_timeout_s:
                state.pop("pending")
                return self._finish(state, al_exit.ExitDecision(al_exit.HITL_DENIED, "승인 대기 시간 초과"))
            if decision != "approve":
                return self._pending(state, pending["calls"], pending["reasons"])
            if edited_arguments:
                _apply_edits(pending, edited_arguments)
                self._event(run_id, "hitl_edit", {"calls": [c["id"] for c in pending["calls"]]})
            state["approved"] = [c["id"] for c in pending["calls"]]
            self._event(run_id, "hitl", {"decision": "approve", "calls": [c["function"]["name"] for c in pending["calls"]]})
            trace.get_current_span().add_event("atlas.hitl.decision", {"decision": "approve", "run_id": run_id})
            await self._act(state, state.pop("pending")["all_calls"])
        self._event(run_id, "resume", {"turns": state["turns"]})
        return await self._loop(state)

    # ---- 루프 ----
    async def _loop(self, state: dict[str, Any]) -> Outcome:
        run_id = state["run_id"]
        while True:
            if d := al_exit.check_budget(self.exit_cfg, state, self.clock()):
                return self._finish(state, d)
            if self.gateway is None:
                return self._finish(state, al_exit.ExitDecision(al_exit.ERROR, "ATLAS_GATEWAY_URL 미설정"))
            self._maybe_compact(state)
            tools = [self.tools[n].openai() for n in state["tools"] if n in self.tools]
            try:
                msg, usage, cost = await self.gateway.chat(state["messages"], tools, f"agent_id:al-run,unit_id:AL-RUN,tenant:{state['tenant']}",
                                                           task_id=f"run:{run_id}")
            except GuardBlocked as exc:
                return self._finish(state, al_exit.ExitDecision(al_exit.POLICY_VIOLATION, str(exc)))
            except BudgetExceeded as exc:
                reason = al_exit.BUDGET_TOKENS if exc.code == "task_budget_exceeded" else al_exit.BUDGET_USD
                return self._finish(state, al_exit.ExitDecision(reason, f"gateway {exc.code}: {exc}"))
            except GatewayError as exc:
                return self._finish(state, al_exit.ExitDecision(al_exit.ERROR, str(exc)))
            state["llm_calls"] += 1
            state["usage"]["input"] += usage["input"]
            state["usage"]["output"] += usage["output"]
            state["cost_usd"] = str(Decimal(state["cost_usd"]) + cost)
            prev = state.get("last_call") or {}
            tok = usage["input"] + usage["output"]                                            # 다음 호출 예측 (EXIT-04): 맥락이 자라므로 직전 + 증가분
            state["last_call"] = {"tokens": tok + max(0, tok - int(prev.get("raw", tok))), "raw": tok,
                                  "usd": str(cost + max(Decimal(0), cost - Decimal(prev.get("raw_usd", str(cost))))), "raw_usd": str(cost)}
            self._event(run_id, "usage", {"call": tok, "used": state["usage"]["input"] + state["usage"]["output"], "next_estimate": state["last_call"]["tokens"]})
            calls = msg.get("tool_calls") or []

            if not calls:                                                                     # ② end_turn
                output = msg.get("content") or ""
                state["messages"].append({"role": "assistant", "content": output})
                state["output"] = output
                v = al_ref.assess_final(output, state["dod"])                               # REFLECT
                self._event(run_id, "reflect", {"decision": v.decision, "dod": [d.__dict__ for d in v.dod]})
                if v.decision == al_ref.DONE_CANDIDATE:
                    return self._finish(state, al_exit.ExitDecision(al_exit.GOAL_VERIFIED, "DoD 검증 통과"))   # ①
                if not v.checkable and self.exit_cfg.require_external_verify:   # 거짓 성공 방지 (REF-06)
                    state["turns"] += 1
                    state["verify_failures"] += 1
                    if state["verify_failures"] >= 3:
                        return self._finish(state, al_exit.ExitDecision(al_exit.MAX_TURNS, "외부 검증 없이 완료를 3회 주장 — 검증 가능한 DoD 필요"))
                    state["messages"].append({"role": "user", "content": "완료 주장에 외부 검증 근거가 없습니다. DoD 를 기계적으로 확인할 수 있게(도구 결과·형식) 답하세요."})
                    self._event(run_id, "reflect", {"decision": "unverified_completion", "failures": state["verify_failures"]})
                    self.store.checkpoint(state["tenant"], run_id, "reflecting", state)
                    continue
                if self.exit_cfg.end_turn_policy == "accept" or not v.checkable:
                    return self._finish(state, al_exit.ExitDecision(al_exit.END_TURN, "기계 검증 가능한 DoD 없음" if not v.checkable else ""))
                state["turns"] += 1
                state["verify_failures"] += 1
                state["messages"].append({"role": "user", "content": "DoD 미충족: " + "; ".join(v.reasons) + " — 다시 시도하세요."})
                self.store.checkpoint(state["tenant"], run_id, "reflecting", state)
                continue

            state["turns"] += 1
            state["messages"].append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            loop = al_exit.record_calls(self.exit_cfg, state, calls)                         # ⑥ 반복 탐지
            if loop == "exit":
                return self._finish(state, al_exit.ExitDecision(al_exit.LOOP_DETECTED, "재계획 후에도 같은 호출 반복"))
            if loop == "replan":
                state["plan"] = await self.planner.revise(state["tenant"], state["plan"], "같은 도구·인자 반복 — 다른 접근 필요")
                self._event(run_id, "replan", {"cause": "loop", "version": state["plan"].get("version")})
            n0 = len(state["messages"])
            try:
                await self._act(state, calls)                                                  # ACT · OBSERVE
            except ApprovalNeeded as need:
                return self._pending(state, need.calls, need.reasons, all_calls=calls)
            grown = sum(len(str(m.get("content") or "")) for m in state["messages"][n0:]) // 4   # 관측이 다음 입력에 더해짐 (≈4자/토큰)
            state["last_call"]["tokens"] = max(state["last_call"]["tokens"], tok + usage["output"] + grown)
            self.store.checkpoint(state["tenant"], run_id, "observing", state)

    async def _act(self, state: dict[str, Any], calls: list[dict[str, Any]]) -> None:
        run_id = state["run_id"]
        actor = Actor(self.tools, self.act_cfg, dedupe_get=self.store.dedupe_get, dedupe_put=self.store.dedupe_put,
                      store_artifact=lambda text: self.store.put_artifact(run_id, text), policy=self.policy)
        for c in calls:
            self._event(run_id, "tool_call", {"id": c["id"], "name": c["function"]["name"]})
        observations = await actor.execute(calls, run_id=run_id, step=state["turns"], ctx=state["ctx"], approved=set(state["approved"]))
        ok = [o for o in observations if o.status == al_obs.OK]
        if self.guard and ok:                                                                # tool_result 경계: PII 정화·spotlight
            texts, rule, decision = await self.guard.tool_result([o.content for o in ok], session=run_id)
            for o, t in zip(ok, texts, strict=True):
                o.content = t
            if rule and not state.get("spotlight"):
                state["messages"][0]["content"] += "\n\n" + rule
                state["spotlight"] = True
            self._event(run_id, "guard", {"boundary": "tool_result", "decision": decision})
        for o in observations:
            state["messages"].append(o.to_message())
            self._event(run_id, "observation", {"id": o.tool_call_id, "tool": o.tool, "status": o.status, "replayed": o.replayed,
                                                "truncated": bool(o.resource_link)})
        loaded = [n for n in state["ctx"].pop("loaded_tools", []) if n in self.tools and n not in state["tools"]]
        if loaded:                                                                           # load_tools → 다음 턴부터 노출 (FR-MCP-SEL-03)
            state["tools"].extend(loaded)
            self._event(run_id, "tools_loaded", {"names": loaded})
        v = al_ref.assess_step([o.to_dict() for o in observations])                          # REFLECT
        self._event(run_id, "reflect", {"decision": v.decision, "reasons": v.reasons})
        if v.decision == al_ref.REVISE:                                                      # 관찰 기반 재계획 (FR-AL-PLAN-05)
            state["plan"] = await self.planner.revise(state["tenant"], state["plan"], "; ".join(v.reasons))
            self._event(run_id, "replan", {"cause": "observation", "version": state["plan"].get("version")})

    # ---- 압축 (FR-AL-RUN-05, client 전략) ----
    def _maybe_compact(self, state: dict[str, Any]) -> None:
        msgs = state["messages"]
        limit = int(self.compact_cfg.context_window * self.compact_cfg.trigger_ratio)
        if est_tokens(msgs) <= limit or len(msgs) <= self.compact_cfg.keep_last + 2:
            return
        split = len(msgs) - self.compact_cfg.keep_last
        while split > 2 and msgs[split]["role"] == "tool":   # tool 응답과 그 호출을 갈라놓지 않는다
            split -= 1
        old = msgs[2:split]
        notes = []
        for m in old:
            if m.get("tool_calls"):
                notes.append("호출: " + ", ".join(f"{c['function']['name']}{c['function'].get('arguments', '')}" for c in m["tool_calls"]))
            elif m["role"] == "tool":
                notes.append("결과: " + m["content"][:160])
            elif m.get("content"):
                notes.append(f"{m['role']}: {m['content'][:160]}")
        summary = {"role": "user", "content": "[이전 대화 요약 — 압축됨]\n" + "\n".join(notes)}
        before = est_tokens(msgs)
        state["messages"] = msgs[:2] + [summary] + msgs[split:]
        state["compactions"] = state.get("compactions", 0) + 1
        self._event(state["run_id"], "compact", {"before_tokens": before, "after_tokens": est_tokens(state["messages"]), "dropped": len(old)})

    # ---- 종료 ----
    def _system(self, plan: dict[str, Any], dod: list[dict[str, Any]]) -> str:
        steps = "\n".join(f"- {s['id']}: {s['title']}" for s in plan.get("steps", []))
        done = "\n".join(f"- [{d.get('type')}] {d.get('spec')}" for d in dod)
        return f"{SYSTEM}\n\nPlan:\n{steps}\n\nDefinition of done:\n{done}"

    def _pending(self, state: dict[str, Any], calls: list[dict[str, Any]], reasons: list[str], all_calls: list[dict[str, Any]] | None = None) -> Outcome:
        prev = state.get("pending") or {}
        state["pending"] = {"calls": calls, "reasons": reasons, "all_calls": all_calls or prev.get("all_calls", calls),
                            "since": prev.get("since", self.clock())}
        decision = al_exit.ExitDecision(al_exit.HITL_PENDING, "; ".join(reasons))
        return Outcome(decision, self._finish(state, decision, final=False).result, pending=calls)

    def _finish(self, state: dict[str, Any], d: al_exit.ExitDecision, final: bool = True) -> Outcome:
        run_id, tenant = state["run_id"], state["tenant"]
        seq = self.store.checkpoint(tenant, run_id, "exit" if final else "hitl", state)
        out = state.get("output", "")
        result = {"run_id": run_id, "status": d.status, "reason": d.reason, "detail": d.detail, "turns": state["turns"],
                  "llm_calls": state["llm_calls"], "usage": state["usage"], "cost_usd": state["cost_usd"],
                  "output" if d.status == "success" else "partial_output": "" if d.reason == al_exit.POLICY_VIOLATION else out,
                  "checkpoint_id": seq, "plan_id": state["plan"].get("id"), "compactions": state.get("compactions", 0)}
        if state.get("pending"):
            result["pending_approval"] = [{"id": c["id"], "tool": c["function"]["name"], "arguments": c["function"].get("arguments")}
                                          for c in state["pending"]["calls"]]
        self.store.finish_run(tenant, run_id, d.status, d.reason, state["turns"], result)
        self._event(run_id, "exit", {"reason": d.reason, "status": d.status, "detail": d.detail})
        span = trace.get_current_span()            # 결정 이벤트를 trace 에 연결 (FR-OB-LOG-06)
        span.add_event("atlas.loop.exit", {"reason": d.reason, "status": d.status, "turns": state["turns"], "run_id": run_id})
        span.set_attribute("atlas.loop.exit_reason", d.reason)
        return Outcome(d, result)

    def _event(self, run_id: str, type_: str, data: dict[str, Any]) -> None:
        self.store.add_event(run_id, type_, data)


def _apply_edits(pending: dict[str, Any], edited: Any) -> None:
    """승인자가 고친 인자를 대기 호출(그리고 실행될 전체 호출)에 반영한다."""
    ids = [c["id"] for c in pending["calls"]]
    by_id = edited if isinstance(edited, dict) and set(edited) <= set(ids) and all(isinstance(v, dict) for v in edited.values()) and edited \
        else ({ids[0]: edited} if len(ids) == 1 and isinstance(edited, dict) else {})
    for c in [*pending["calls"], *pending.get("all_calls", [])]:
        if c["id"] in by_id:
            c["function"]["arguments"] = json.dumps(by_id[c["id"]], ensure_ascii=False)
