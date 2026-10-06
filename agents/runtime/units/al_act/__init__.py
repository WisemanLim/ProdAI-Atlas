"""AL-ACT Actor — 도구 호출 검증·권한·병렬·멱등·재시도 (FR-AL-ACT-01·02·03·05)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from units import al_obs
from units.al_act.tools import BUILTINS, RetryableToolError, ToolSpec

__all__ = ["BUILTINS", "ActConfig", "Actor", "ApprovalNeeded", "RetryableToolError", "ToolSpec", "idempotency_key"]


@dataclass
class ActConfig:
    permission_mode: str = "default"   # default | acceptEdits | plan | bypass (FR-AL-ACT-03)
    max_parallel_tools: int = 8
    max_retries: int = 3
    backoff_base_s: float = 0.5
    tool_timeout_s: float = 30.0
    max_tokens_per_result: int = 8000


class ApprovalNeeded(Exception):
    """사람 승인이 필요한 호출 — 루프는 HITL_PENDING 으로 멈춘다 (FR-AL-EXIT-05)."""

    def __init__(self, calls: list[dict[str, Any]], reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.calls, self.reasons = calls, reasons


def canonical(args: Any) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def idempotency_key(run_id: str, step: int, tool: str, args: Any) -> str:
    return hashlib.sha256(f"{run_id}|{step}|{tool}|{canonical(args)}".encode()).hexdigest()


# 정책 훅: (tool, args) → "allow" | "deny" | "ask" — GR-POL(Epic 8) 이 여기에 붙는다.
PolicyHook = Callable[[ToolSpec, dict[str, Any]], Awaitable[tuple[str, str]]]


@dataclass
class Actor:
    tools: dict[str, ToolSpec]
    config: ActConfig = field(default_factory=ActConfig)
    dedupe_get: Callable[[str], Any | None] = lambda _k: None        # 멱등 저장소 조회
    dedupe_put: Callable[[str, Any], None] = lambda _k, _v: None
    store_artifact: Callable[[str], str] | None = None
    policy: PolicyHook | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def permission(self, spec: ToolSpec) -> str:
        mode = self.config.permission_mode
        rule = spec.annotations.get("approval")   # MCP 승인 규칙 (FR-MCP-HOST-06)
        if rule == "deny":
            return "deny"
        if rule == "require_approval" and mode != "bypass":
            return "ask"
        if mode == "bypass":
            return "allow"
        if mode == "plan":
            return "deny" if spec.side_effect != "read" else "allow"
        if spec.side_effect == "irreversible" or spec.risk == "high":
            return "ask"  # 되돌릴 수 없거나 고위험 → 사람 승인
        return "allow"

    async def execute(self, calls: list[dict[str, Any]], *, run_id: str, step: int, ctx: dict[str, Any],
                      approved: set[str] | None = None) -> list[al_obs.Observation]:
        """calls = OpenAI tool_calls. 승인 필요 호출이 있으면 아무것도 실행하지 않고 ApprovalNeeded."""
        approved = approved or set()
        prepared, results, needs, reasons = [], {}, [], []
        for c in calls:
            cid, name = c["id"], c["function"]["name"]
            spec = self.tools.get(name)
            if spec is None:
                results[cid] = al_obs.error(cid, name, al_obs.ERROR, "unknown_tool", f"도구 {name} 없음",
                                            hint=f"사용 가능: {', '.join(sorted(self.tools))}")
                continue
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
                jsonschema.validate(args, spec.input_schema)
            except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
                msg = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
                results[cid] = al_obs.error(cid, name, al_obs.ERROR, "invalid_arguments", msg, hint="inputSchema 에 맞게 인자를 고쳐 다시 호출")
                continue
            decision, why = ("allow", "")
            if self.policy:
                decision, why = await self.policy(spec, args)
            if decision == "allow":
                p = self.permission(spec)
                decision, why = (p, f"{name}: {spec.side_effect}/{spec.risk} 는 승인 필요") if p != "allow" else (p, why)
            if decision == "deny":
                results[cid] = al_obs.error(cid, name, al_obs.DENIED, "permission_denied", why or f"{name} 실행이 정책상 거부됨")
                continue
            if decision == "ask" and cid not in approved:
                needs.append(c)
                reasons.append(why or f"{name} 승인 필요")
                continue
            prepared.append((c, spec, args))
        if needs:
            raise ApprovalNeeded(needs, reasons)

        sem = asyncio.Semaphore(self.config.max_parallel_tools)

        async def run_one(c: dict, spec: ToolSpec, args: dict) -> al_obs.Observation:
            async with sem:
                return await self._invoke(c["id"], spec, args, run_id=run_id, step=step, ctx=ctx)

        reads = [p for p in prepared if p[1].side_effect == "read"]
        writes = [p for p in prepared if p[1].side_effect != "read"]
        for obs in await asyncio.gather(*(run_one(*p) for p in reads)):   # 읽기는 병렬 (FR-AL-ACT-02)
            results[obs.tool_call_id] = obs
        for p in writes:                                                    # 쓰기는 직렬
            obs = await run_one(*p)
            results[obs.tool_call_id] = obs
        return [results[c["id"]] for c in calls]

    async def _invoke(self, cid: str, spec: ToolSpec, args: dict, *, run_id: str, step: int, ctx: dict) -> al_obs.Observation:
        key = idempotency_key(run_id, step, spec.name, args)
        if spec.side_effect != "read" and (prev := self.dedupe_get(key)) is not None:   # 재생 — 부작용 중복 방지
            obs = al_obs.normalize(cid, spec.name, prev, max_tokens=self.config.max_tokens_per_result, store=self.store_artifact)
            obs.replayed = True
            return obs
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await asyncio.wait_for(spec.handler(args, ctx), self.config.tool_timeout_s)
                break
            except RetryableToolError as exc:
                if attempt > self.config.max_retries:
                    return al_obs.error(cid, spec.name, al_obs.ERROR, "retry_exhausted", str(exc), retryable=True)
                await self.sleep(self.config.backoff_base_s * 2 ** (attempt - 1))
            except TimeoutError:
                return al_obs.error(cid, spec.name, al_obs.TIMEOUT, "timeout", f"{self.config.tool_timeout_s}s 초과", retryable=True)
            except Exception as exc:  # noqa: BLE001 — 도구 오류는 관찰로 돌려준다
                return al_obs.error(cid, spec.name, al_obs.ERROR, type(exc).__name__, str(exc)[:500], hint="인자나 접근 방식을 바꿔 재시도")
        if spec.side_effect != "read":
            self.dedupe_put(key, result)
        obs = al_obs.normalize(cid, spec.name, result, max_tokens=self.config.max_tokens_per_result, store=self.store_artifact)
        obs.meta["attempts"] = attempt
        return obs
