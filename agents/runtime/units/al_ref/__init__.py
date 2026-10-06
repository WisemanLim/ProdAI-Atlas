"""AL-REF Reflector — 진척 평가와 규칙 기반 DoD 검증 (FR-AL-REF-01·02).

기계 검증 가능한 DoD: type=test(spec "contains:…" | "regex:…" | "equals:…") · type=schema(spec=JSON Schema).
type=judge(EV-JUDGE, M3)·human 은 기계 검증 불가로 분류한다. 샌드박스 테스트 러너(pytest 등)는 X-SBX(M3).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

CONTINUE, REVISE, DONE_CANDIDATE = "continue", "revise", "done_candidate"


@dataclass
class DodResult:
    type: str
    spec: Any
    checkable: bool
    passed: bool | None
    reason: str = ""


@dataclass
class Verdict:
    decision: str
    reasons: list[str] = field(default_factory=list)
    dod: list[DodResult] = field(default_factory=list)

    @property
    def checkable(self) -> bool:
        return any(d.checkable for d in self.dod)

    @property
    def dod_passed(self) -> bool:
        checked = [d for d in self.dod if d.checkable]
        return bool(checked) and all(d.passed for d in checked)


def check_dod(item: dict[str, Any], output: str) -> DodResult:
    kind, spec = item.get("type"), item.get("spec")
    if kind == "test" and isinstance(spec, str):
        op, _, arg = spec.partition(":")
        if op == "contains":
            return DodResult(kind, spec, True, arg in output, "" if arg in output else f"출력에 '{arg}' 없음")
        if op == "equals":
            ok = output.strip() == arg
            return DodResult(kind, spec, True, ok, "" if ok else "출력이 기대값과 다름")
        if op == "regex":
            ok = re.search(arg, output) is not None
            return DodResult(kind, spec, True, ok, "" if ok else f"정규식 {arg} 불일치")
    if kind == "schema" and isinstance(spec, dict):
        try:
            jsonschema.validate(json.loads(output), spec)
            return DodResult(kind, spec, True, True)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            return DodResult(kind, spec, True, False, getattr(exc, "message", str(exc))[:200])
    return DodResult(str(kind), spec, False, None, "기계 검증 불가(judge·human·자유 서술)")


def assess_step(observations: list[dict[str, Any]]) -> Verdict:
    """도구 턴 후 진척 판정 — 재시도 불가 오류·거부가 있으면 재계획(revise) 후보."""
    bad = [o for o in observations if o["status"] in ("error", "denied") and not (o.get("error") or {}).get("retryable")]
    if bad:
        return Verdict(REVISE, [f"{o['tool']}: {(o.get('error') or {}).get('error_type', o['status'])}" for o in bad])
    return Verdict(CONTINUE)


def assess_final(output: str, dod: list[dict[str, Any]]) -> Verdict:
    """모델 최종 답에 DoD 를 적용한다. 기계 검증 항목이 모두 통과하면 done_candidate."""
    results = [check_dod(d, output) for d in dod]
    v = Verdict(CONTINUE, dod=results)
    if v.dod_passed:
        v.decision = DONE_CANDIDATE
    elif v.checkable:
        v.decision, v.reasons = REVISE, [r.reason for r in results if r.checkable and not r.passed]
    return v
