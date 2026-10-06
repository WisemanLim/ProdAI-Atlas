"""GR-IO 경계별 검사 파이프라인 (FR-GR-IO-01·02·09) — stage = {checker, mode block|monitor|transform|shadow, tier, timeout_ms, on_error}.

tier 오름차순 실행. block 모드 검사기가 차단하면 이후 단계 생략(cascade). tier ≥ 2(LLM 판정 등 비싼 검사)는
tier 0 결과가 모두 확정적이면 생략한다.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from units.gr_io.checkers import StageResult, run_checker

BOUNDARIES = ("input", "output", "tool_call", "tool_result", "retrieval")
ALLOW, BLOCK, TRANSFORM, REQUIRE_APPROVAL = "allow", "block", "transform", "require_approval"


@dataclass
class Verdict:
    boundary: str
    decision: str
    contents: list[str]
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    spotlight_rule: str | None = None
    latency_ms: float = 0.0


async def _run_stage(stage: dict[str, Any], text: str, ctx: dict[str, Any]) -> StageResult:
    timeout = float(stage.get("timeout_ms", 200)) / 1000
    try:
        return await asyncio.wait_for(asyncio.to_thread(run_checker, stage, text, ctx), timeout)
    except Exception as exc:  # noqa: BLE001 — 검사기 오류는 on_error 정책으로
        closed = stage.get("on_error", "fail_closed" if stage["mode"] == "block" else "fail_open") == "fail_closed"
        return StageResult(stage["checker"], closed, 1.0 if closed else 0.0, ["checker_error"], detail=f"{type(exc).__name__}: {exc}"[:200])


async def run(bundle: dict[str, Any], boundary: str, contents: list[str], ctx: dict[str, Any],
              excepted: set[str] | None = None) -> Verdict:
    """contents = 검사할 텍스트들(tool_call 경계는 [""] 와 ctx.tool·args). excepted = 예외 처리된 checker 이름."""
    if boundary not in BOUNDARIES:
        raise ValueError(f"boundary {boundary}")
    started = time.perf_counter()
    stages = sorted(bundle["boundaries"].get(boundary, []), key=lambda s: s.get("tier", 0))
    ctx["boundary"] = boundary
    out = list(contents)
    v = Verdict(boundary, ALLOW, out)
    tier0_decisive = True
    for stage in stages:
        if stage["checker"] in (excepted or set()):
            v.stages.append({"checker": stage["checker"], "result": "excepted"})
            continue
        if stage.get("tier", 0) >= 2 and tier0_decisive and v.decision == ALLOW:
            v.stages.append({"checker": stage["checker"], "result": "skipped_cascade"})
            continue
        for i, text in enumerate(out):
            t0 = time.perf_counter()
            ctx["_index"] = i
            r = await _run_stage(stage, text, ctx)
            ms = round((time.perf_counter() - t0) * 1000, 2)
            if stage.get("tier", 0) == 0 and not r.decisive:
                tier0_decisive = False
            rec = {"checker": r.checker, "mode": stage["mode"], "flagged": r.flagged, "score": r.score, "categories": r.categories,
                   "ms": ms, "index": i, **({"detail": r.detail} if r.detail else {})}
            if stage["mode"] == "shadow":                     # 섀도: 판정만 기록, 결정·내용에 영향 없음 (GR-IO-09)
                rec["shadow"] = True
                v.stages.append(rec)
                continue
            v.stages.append(rec)
            if r.approval:
                v.decision = REQUIRE_APPROVAL
                v.reasons.append(r.detail or "승인 필요")
            if not r.flagged:
                continue
            v.categories = sorted({*v.categories, *r.categories})
            if stage["mode"] == "block":
                v.decision = BLOCK
                v.reasons.append(f"{r.checker}: {', '.join(r.categories) or r.detail}")
                v.latency_ms = round((time.perf_counter() - started) * 1000, 2)
                return v                                   # cascade — 이후 단계 생략
            if stage["mode"] == "transform" and r.transformed is not None:
                out[i] = r.transformed
                if v.decision == ALLOW:
                    v.decision = TRANSFORM
        if stage["checker"] == "spotlight":
            v.spotlight_rule = ctx.get("spotlight_rule")
    v.latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return v


def to_dict(v: Verdict) -> dict[str, Any]:
    return asdict(v)
