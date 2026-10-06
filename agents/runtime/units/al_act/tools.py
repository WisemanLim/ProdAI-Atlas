"""AL-ACT 내장 도구 — 데모·진단용. MCP 도구(Epic 7)도 같은 ToolSpec 으로 감싼다."""
from __future__ import annotations

import ast
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class RetryableToolError(Exception):
    """일시 오류 — 지수 백오프로 재시도한다 (FR-AL-ACT-05)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]  # (args, ctx) → result
    side_effect: str = "read"   # read | write | irreversible
    risk: str = "low"           # low | med | high
    source: str = "builtin"     # builtin | mcp:<server>
    annotations: dict[str, Any] = field(default_factory=dict)

    def openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.input_schema}}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(expr: str) -> float | int:
    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            if isinstance(node.op, ast.Pow) and abs(ev(node.right)) > 64:
                raise ValueError("지수가 너무 큼")
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"허용되지 않는 식: {ast.dump(node)[:40]}")
    return ev(ast.parse(expr, mode="eval"))


async def _echo(args: dict, _: dict) -> str:
    return str(args.get("text", ""))


async def _calc(args: dict, _: dict) -> str:
    return str(safe_eval(args["expr"]))


async def _kv_put(args: dict, ctx: dict) -> dict:
    ctx.setdefault("kv", {})[args["key"]] = args["value"]
    return {"stored": args["key"]}


async def _delete_all(args: dict, ctx: dict) -> dict:
    ctx["kv"] = {}
    return {"deleted": args.get("scope", "all")}


async def _flaky(args: dict, ctx: dict) -> str:
    n = ctx.setdefault("flaky_calls", 0) + 1
    ctx["flaky_calls"] = n
    if n <= int(args.get("fail_times", 1)):
        raise RetryableToolError(f"일시 오류 {n}회차")
    return f"ok after {n} attempts"


BUILTINS: list[ToolSpec] = [
    ToolSpec("atlas_echo", "입력 텍스트를 그대로 돌려준다", {"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": False}, _echo),
    ToolSpec("atlas_calc", "사칙연산 식을 계산한다", {"type": "object", "properties": {"expr": {"type": "string", "maxLength": 200}}, "required": ["expr"], "additionalProperties": False}, _calc),
    ToolSpec("atlas_kv_put", "실행 상태 저장소에 값을 쓴다", {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}, "required": ["key", "value"]}, _kv_put,
             side_effect="write", risk="med"),
    ToolSpec("atlas_delete_all", "저장소를 모두 지운다(되돌릴 수 없음)", {"type": "object", "properties": {"scope": {"type": "string"}}}, _delete_all,
             side_effect="irreversible", risk="high"),
    ToolSpec("atlas_flaky", "진단용 — 처음 fail_times 회는 일시 오류", {"type": "object", "properties": {"fail_times": {"type": "integer", "minimum": 0, "maximum": 5}}}, _flaky),
]
