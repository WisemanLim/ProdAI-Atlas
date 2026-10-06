"""trace 검색 필터 DSL (FR-OB-TRACE-07).

  gen_ai.request.model = "planner-default" AND atlas.cost.usd > 0.0001 AND status = error AND name ~ "chat"

절(clause)은 AND 로만 잇는다. 연산자 = != > >= < <= ~(포함). 키는 내장(service·name·status·duration_ms·trace_id)
또는 span 속성 이름. 값은 큰따옴표 문자열 또는 숫자. 키는 정규식으로 검증하고 값은 모두 파라미터 바인딩한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CLAUSE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(=|!=|>=|<=|>|<|~)\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_.-]*)\s*$')
BUILTIN = {"service": "ServiceName", "name": "SpanName", "status": "StatusCode", "trace_id": "TraceId"}
STATUS = {"error": "Error", "ok": "Ok", "unset": "Unset"}


class DslError(ValueError):
    pass


@dataclass
class Compiled:
    where: str
    params: dict[str, str]


def split_and(q: str) -> list[str]:
    parts, buf, quote = [], "", False
    tokens = re.split(r'(\s+AND\s+|")', q, flags=re.IGNORECASE)
    for t in tokens:
        if t == '"':
            quote = not quote
            buf += t
        elif not quote and re.fullmatch(r"\s+AND\s+", t, re.IGNORECASE):
            parts.append(buf)
            buf = ""
        else:
            buf += t
    if quote:
        raise DslError("따옴표가 닫히지 않음")
    parts.append(buf)
    return [p for p in parts if p.strip()]


def compile_query(q: str, prefix: str = "q") -> Compiled:
    if not q or not q.strip():
        return Compiled("1", {})
    conds, params = [], {}
    for i, clause in enumerate(split_and(q)):
        m = _CLAUSE.match(clause)
        if not m:
            raise DslError(f"절 해석 실패: {clause.strip()!r}")
        key, op, raw = m.groups()
        val = raw[1:-1].replace('\\"', '"') if raw.startswith('"') else raw
        p = f"{prefix}{i}"
        numeric = re.fullmatch(r"-?\d+(\.\d+)?", val) is not None and not raw.startswith('"')
        if key == "duration_ms":
            if not numeric or op == "~":
                raise DslError("duration_ms 는 숫자 비교만")
            conds.append(f"Duration / 1e6 {op} {{{p}:Float64}}")
            params[p] = val
            continue
        if key == "status":
            val = STATUS.get(val.lower(), val)
        col = BUILTIN.get(key)
        expr = col if col else f"SpanAttributes[{{{p}k:String}}]"
        if not col:
            params[f"{p}k"] = key
        if op == "~":
            conds.append(f"positionCaseInsensitive({expr}, {{{p}:String}}) > 0")
        elif numeric and op in (">", ">=", "<", "<=", "=", "!="):
            conds.append(f"toFloat64OrNull({expr}) {op} {{{p}:Float64}}")
        elif op in ("=", "!="):
            conds.append(f"{expr} {op} {{{p}:String}}")
        else:
            raise DslError(f"{key}: 문자열 값에는 = != ~ 만")
        params[p] = val
    return Compiled(" AND ".join(conds), params)
