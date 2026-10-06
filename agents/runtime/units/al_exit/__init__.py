"""AL-EXIT Exit Controller — 종료 조건 판정과 표준 종료 사유 (FR-AL-EXIT-01·02·03·05·06·07·08)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# FR-AL-EXIT-08 ExitReason
GOAL_VERIFIED, END_TURN, MAX_TURNS, BUDGET_TOKENS, BUDGET_USD, TIMEOUT = (
    "GOAL_VERIFIED", "END_TURN", "MAX_TURNS", "BUDGET_TOKENS", "BUDGET_USD", "TIMEOUT")
HITL_PENDING, HITL_DENIED, USER_ABORT, LOOP_DETECTED, POLICY_VIOLATION, ERROR = (
    "HITL_PENDING", "HITL_DENIED", "USER_ABORT", "LOOP_DETECTED", "POLICY_VIOLATION", "ERROR")
REASONS = (GOAL_VERIFIED, END_TURN, MAX_TURNS, BUDGET_TOKENS, BUDGET_USD, TIMEOUT,
           HITL_PENDING, HITL_DENIED, USER_ABORT, LOOP_DETECTED, POLICY_VIOLATION, ERROR)

STATUS = {GOAL_VERIFIED: "success", END_TURN: "success", MAX_TURNS: "truncated", BUDGET_TOKENS: "truncated",
          BUDGET_USD: "truncated", TIMEOUT: "truncated", LOOP_DETECTED: "truncated", HITL_PENDING: "pending",
          HITL_DENIED: "aborted", USER_ABORT: "aborted", POLICY_VIOLATION: "blocked", ERROR: "failed"}


@dataclass
class ExitConfig:
    max_turns: int = 25
    max_llm_calls: int = 50
    max_tokens: int = 0          # 0 = 무제한
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_usd: float = 0.0
    require_external_verify: bool = False   # FR-AL-REF-06: 기계 검증 DoD 통과 없이는 완료 불가
    timeout_s: float = 600.0
    repeat_window: int = 6
    repeat_threshold: int = 3
    end_turn_policy: str = "verify_then_accept"   # accept | verify_then_accept
    hitl_timeout_s: float = 86400.0


@dataclass
class ExitDecision:
    reason: str
    detail: str = ""

    @property
    def status(self) -> str:
        return STATUS[self.reason]


def call_hash(name: str, arguments: str) -> str:
    try:
        args = json.dumps(json.loads(arguments or "{}"), sort_keys=True, ensure_ascii=False)
    except json.JSONDecodeError:
        args = arguments
    return hashlib.sha256(f"{name}|{args}".encode()).hexdigest()[:16]


def check_budget(cfg: ExitConfig, state: dict[str, Any], now: float) -> ExitDecision | None:
    """LLM 호출 전 상한 검사 — 턴·호출 수·토큰·금액·시간 (③④)."""
    if state["turns"] >= cfg.max_turns:
        return ExitDecision(MAX_TURNS, f"turns {state['turns']} ≥ {cfg.max_turns}")
    if state["llm_calls"] >= cfg.max_llm_calls:
        return ExitDecision(MAX_TURNS, f"llm_calls {state['llm_calls']} ≥ {cfg.max_llm_calls}")
    used = state["usage"]["input"] + state["usage"]["output"]
    if cfg.max_tokens and used >= cfg.max_tokens:
        return ExitDecision(BUDGET_TOKENS, f"tokens {used} ≥ {cfg.max_tokens}")
    if cfg.max_input_tokens and state["usage"]["input"] >= cfg.max_input_tokens:
        return ExitDecision(BUDGET_TOKENS, f"input tokens {state['usage']['input']} ≥ {cfg.max_input_tokens}")
    if cfg.max_output_tokens and state["usage"]["output"] >= cfg.max_output_tokens:
        return ExitDecision(BUDGET_TOKENS, f"output tokens {state['usage']['output']} ≥ {cfg.max_output_tokens}")
    if cfg.max_usd and float(state["cost_usd"]) >= cfg.max_usd:
        return ExitDecision(BUDGET_USD, f"cost {state['cost_usd']} ≥ {cfg.max_usd}")
    # 사전 예측 (FR-AL-EXIT-04): 직전 호출만큼 더 쓰면 넘는다 → 호출하지 않고 선제 종료
    last = state.get("last_call") or {}
    if cfg.max_tokens and last.get("tokens") and used + last["tokens"] > cfg.max_tokens:
        return ExitDecision(BUDGET_TOKENS, f"예상 다음 호출 {last['tokens']} tokens → {used + last['tokens']} > {cfg.max_tokens} (선제 종료)")
    if cfg.max_usd and last.get("usd") and float(state["cost_usd"]) + float(last["usd"]) > cfg.max_usd:
        return ExitDecision(BUDGET_USD, f"예상 다음 호출 ${last['usd']} → 잔여 예산 초과 (선제 종료)")
    if now - state["started_at"] >= cfg.timeout_s:
        return ExitDecision(TIMEOUT, f"{cfg.timeout_s}s 초과")
    return None


def record_calls(cfg: ExitConfig, state: dict[str, Any], calls: list[dict[str, Any]]) -> str | None:
    """반복 탐지 (FR-AL-EXIT-06): 최근 window 내 같은 (도구, 인자)가 threshold 회 이상이면
    1차는 'replan', 이미 재계획했으면 'exit'."""
    recent: list[str] = state.setdefault("recent_calls", [])
    for c in calls:
        recent.append(call_hash(c["function"]["name"], c["function"].get("arguments", "")))
    del recent[:-cfg.repeat_window]
    worst = max((recent.count(h) for h in set(recent)), default=0)
    if worst < cfg.repeat_threshold:
        return None
    if state.get("replanned_for_loop"):
        return "exit"
    state["replanned_for_loop"] = True
    recent.clear()
    return "replan"
