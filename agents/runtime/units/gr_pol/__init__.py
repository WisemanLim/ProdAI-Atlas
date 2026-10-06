"""GR-POL Policy Enforcement — 정책 as code 번들 · 평가 엔진(PDP) · 도구 인가 · 비가역 HITL · 기본 정책 팩
(FR-GR-POL-01·02·04·05·07). OPA/Rego·Colang 연동은 후속 — 조건식은 안전한 부분 Python 식(ast 화이트리스트)."""
from __future__ import annotations

import ast
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

BASELINE_PATH = Path(__file__).with_name("baseline-v1.yaml")
ALLOW, DENY, REQUIRE_APPROVAL, TRANSFORM = "allow", "deny", "require_approval", "transform"


def baseline() -> dict[str, Any]:
    return yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))


_ALLOWED_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare, ast.Eq, ast.NotEq, ast.Lt,
                  ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Constant, ast.Name, ast.Load, ast.Attribute,
                  ast.Call, ast.Subscript, ast.List, ast.Tuple)
_ALLOWED_CALLS = {"get", "startswith", "endswith", "lower", "str", "len", "int", "float"}


class PolicyError(ValueError):
    pass


def compile_when(expr: str):
    """정책 조건식 컴파일 — args·tool·principal 만 참조, 허용된 메서드만 호출."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise PolicyError(f"허용되지 않는 구문: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in ("args", "tool", "principal", "str", "len", "int", "float", "True", "False", "None"):
            raise PolicyError(f"알 수 없는 이름: {node.id}")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr not in _ALLOWED_CALLS):
            raise PolicyError(f"허용되지 않는 속성: {node.attr}")
        if isinstance(node, ast.Call):
            fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if fn not in _ALLOWED_CALLS:
                raise PolicyError(f"허용되지 않는 호출: {fn}")
    code = compile(tree, "<policy>", "eval")
    return lambda env: eval(code, {"__builtins__": {}, "str": str, "len": len, "int": int, "float": float}, env)


def validate_bundle(bundle: dict[str, Any]) -> None:
    if not isinstance(bundle.get("version"), str) or not isinstance(bundle.get("boundaries"), dict):
        raise PolicyError("version(문자열)·boundaries(객체) 필수")
    for b, stages in bundle["boundaries"].items():
        if b not in ("input", "output", "tool_call", "tool_result", "retrieval"):
            raise PolicyError(f"알 수 없는 경계 {b}")
        for st in stages:
            if st.get("mode") not in ("block", "monitor", "transform", "shadow"):
                raise PolicyError(f"{b}: mode block|monitor|transform|shadow")
    for r in bundle.get("tools", {}).get("rules", []):
        if r.get("action") not in (ALLOW, DENY, REQUIRE_APPROVAL):
            raise PolicyError("tools.rules[].action allow|deny|require_approval")
        if r.get("when"):
            compile_when(r["when"])


@dataclass
class Decision:
    decision: str
    reasons: list[str] = field(default_factory=list)
    rule: str | None = None


def decide_tool(bundle: dict[str, Any], tool: dict[str, Any], args: dict[str, Any], principal: dict[str, Any] | None = None,
                provenance: dict[str, str] | None = None) -> Decision:
    """도구 호출 인가 (FR-GR-POL-02·04·05): 스키마 → 규칙(먼저 deny 가 이김) → 기본값(비가역 승인 · 비신뢰 인자)."""
    name, side = tool.get("name", ""), tool.get("side_effect", "read")
    if schema := tool.get("input_schema"):
        try:
            jsonschema.validate(args, schema)
        except jsonschema.ValidationError as exc:
            return Decision(DENY, [f"인자 스키마 위반: {exc.message}"], "schema")
    env = {"args": args, "tool": tool, "principal": principal or {}}
    approvals = []
    for i, r in enumerate(bundle.get("tools", {}).get("rules", [])):
        if not fnmatch.fnmatch(name, r["match"]):
            continue
        try:
            hit = bool(compile_when(r["when"])(env)) if r.get("when") else True
        except Exception as exc:  # noqa: BLE001 — 조건식 평가 오류는 보수적으로 거부
            return Decision(DENY, [f"정책 평가 오류: {exc}"], f"rules[{i}]")
        if not hit:
            continue
        if r["action"] == DENY:
            return Decision(DENY, [r.get("reason", f"규칙 {r['match']}")], f"rules[{i}]")
        if r["action"] == REQUIRE_APPROVAL:
            approvals.append(r.get("reason", f"규칙 {r['match']}"))
    defaults = bundle.get("tools", {}).get("defaults", {})
    if side == "irreversible" and defaults.get("irreversible") == REQUIRE_APPROVAL:
        approvals.append("비가역 도구")
    if side != "read" and defaults.get("untrusted_args_write") == REQUIRE_APPROVAL and "untrusted" in (provenance or {}).values():
        approvals.append("비신뢰 출처 인자가 쓰기 도구로 전달됨")
    if approvals:
        return Decision(REQUIRE_APPROVAL, approvals, "approval")
    return Decision(ALLOW)
