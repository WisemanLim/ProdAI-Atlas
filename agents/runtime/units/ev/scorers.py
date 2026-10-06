"""코드 채점기 (FR-EV-REG-03) — 결정적 판정. python 샌드박스·state_diff 는 X-SBX 이후."""
from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

from units.ev.suite import dig


def as_text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)


def as_obj(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def score(s: dict[str, Any], output: Any, item: dict[str, Any], latency_ms: float, cost_usd: float) -> dict[str, Any]:
    t = s["type"]
    val = dig(as_obj(output), s["path"]) if s.get("path") else output
    text, expected = as_text(val), item.get("expected_output")
    if t == "contains":
        ok = str(s["value"]) in text
    elif t == "not_contains":
        ok = str(s["value"]) not in text
    elif t == "regex":
        ok = re.search(s["pattern"], text) is not None
    elif t == "equals":
        ok = as_obj(val) == s["value"] or text == str(s["value"])
    elif t == "contains_expected":
        ok = expected is not None and as_text(expected) in text
    elif t == "equals_expected":
        ok = expected is not None and as_obj(val) == as_obj(expected)
    elif t == "json_schema":
        try:
            jsonschema.validate(as_obj(val), s["schema"])
            ok = True
        except jsonschema.ValidationError:
            ok = False
    elif t == "latency_lt_ms":
        ok = latency_ms < float(s["value"])
    elif t == "cost_lt_usd":
        ok = cost_usd < float(s["value"])
    else:
        raise ValueError(t)
    return {"type": t, "passed": bool(ok)}
