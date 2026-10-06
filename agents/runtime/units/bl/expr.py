"""안전한 규칙 식 평가기 (FR-BL-MODEL-04 채택 규칙 DSL · BL-HARN-02 도메인 규칙) — 파이썬 식 부분집합만 허용.

허용: 숫자·문자열·불리언 상수, 변수, and/or/not, 비교(== != < <= > >= in), 산술(+ - * /), 함수 min·max·abs·len.
속성 접근·인덱싱·람다·컴프리헨션·임의 호출은 거부한다 (CEL 대체 — 평가 엔진 의존 없음).
"""
from __future__ import annotations

import ast
import operator
from typing import Any

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_FUN = {"min": min, "max": max, "abs": abs, "len": len}


class ExprError(ValueError):
    pass


def compile_expr(src: str) -> ast.Expression:
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"식 문법 오류: {exc.msg}") from exc
    for n in ast.walk(tree):
        ok = isinstance(n, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Compare, ast.Name,
                            ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Call, *_BIN, *_CMP))
        if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _FUN and not n.keywords):
            ok = False
        if not ok:
            raise ExprError(f"허용되지 않는 구문: {type(n).__name__}")
    return tree


def evaluate(src: str | ast.Expression, env: dict[str, Any]) -> Any:
    tree = compile_expr(src) if isinstance(src, str) else src

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[n.id]
            if n.id not in env:
                raise ExprError(f"알 수 없는 변수: {n.id}")
            return env[n.id]
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.BoolOp):
            vals = (ev(v) for v in n.values)
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return (not v) if isinstance(n.op, ast.Not) else -v
        if isinstance(n, ast.BinOp):
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right in zip(n.ops, n.comparators):
                r = ev(right)
                if not _CMP[type(op)](left, r):
                    return False
                left = r
            return True
        if isinstance(n, ast.Call):
            return _FUN[n.func.id](*[ev(a) for a in n.args])
        raise ExprError(type(n).__name__)
    try:
        return ev(tree)
    except (TypeError, ZeroDivisionError) as exc:
        raise ExprError(str(exc)) from exc
