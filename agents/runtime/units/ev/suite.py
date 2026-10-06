"""eval-as-code 스위트 (FR-EV-REG-01) — atlas-eval.yaml 스키마·lint·템플릿 렌더. promptfoo 설정 import 는 후속."""
from __future__ import annotations

import json
import re
from typing import Any

import jsonschema
import yaml

SCORERS = ["contains", "not_contains", "regex", "equals", "json_schema", "contains_expected", "equals_expected", "latency_lt_ms", "cost_lt_usd",
           "judge"]   # judge = LLM-as-Judge 루브릭 (EV-JUDGE)

SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["name", "dataset", "targets", "scorers"], "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "pattern": "^[a-zA-Z0-9._-]{1,128}$"},
        "description": {"type": "string"},
        "dataset": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "version": {"type": "integer", "minimum": 1}}},
        "targets": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["type"], "properties": {
                "id": {"type": "string"}, "type": {"enum": ["prompt", "agent", "http"]},
                "model": {"type": "string"}, "system": {"type": "string"}, "template": {"type": "string"},
                "response_format": {"enum": ["json_object"]}, "url": {"type": "string"}, "skill": {"type": "string"},
                "data_template": {"type": "object"}, "result_path": {"type": "string"},
                "effort": {"enum": ["minimal", "low", "medium", "high"]}, "metadata": {"type": "object"}},
            "allOf": [{"if": {"properties": {"type": {"const": "prompt"}}}, "then": {"required": ["model", "template"]}},
                      {"if": {"properties": {"type": {"enum": ["agent", "http"]}}}, "then": {"required": ["url"]}}]}},
        "scorers": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["type"], "properties": {
                "type": {"enum": SCORERS}, "value": {}, "pattern": {"type": "string"}, "schema": {"type": "object"}, "path": {"type": "string"},
                "judge": {"type": "string"}, "version": {"type": "integer", "minimum": 1}, "allow_uncalibrated": {"type": "boolean"}}}},
        "epochs": {"type": "integer", "minimum": 1, "maximum": 10},
        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 64},
        "thresholds": {"type": "object", "properties": {"pass_rate": {"type": "number", "minimum": 0, "maximum": 1},
                                                         "pass_hat_k": {"type": "number", "minimum": 0, "maximum": 1},
                                                         "max_drop_pp": {"type": "number", "minimum": 0}}},
        "baseline_run_id": {"type": "string"},
    },
}


def load(text_or_obj: str | dict[str, Any]) -> dict[str, Any]:
    suite = yaml.safe_load(text_or_obj) if isinstance(text_or_obj, str) else text_or_obj
    lint(suite)
    suite.setdefault("epochs", 1)
    suite.setdefault("thresholds", {"pass_rate": 1.0})
    for i, t in enumerate(suite["targets"]):
        t.setdefault("id", f"{t['type']}-{i}")
    return suite


def lint(suite: Any) -> None:
    errors = sorted(jsonschema.Draft202012Validator(SCHEMA).iter_errors(suite), key=lambda e: list(e.path))
    if errors:
        raise ValueError("; ".join(f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in errors))
    for s in suite["scorers"]:
        if s["type"] in ("contains", "not_contains", "equals", "latency_lt_ms", "cost_lt_usd") and "value" not in s:
            raise ValueError(f"scorer {s['type']}: value 필요")
        if s["type"] == "regex":
            re.compile(s.get("pattern", ""))
        if s["type"] == "judge" and not s.get("judge"):
            raise ValueError("scorer judge: judge(이름) 필요")
        if s["type"] == "json_schema" and "schema" not in s:
            raise ValueError("scorer json_schema: schema 필요")


_VAR = re.compile(r"\{\{\s*(input|expected)(?:\.([A-Za-z0-9_.]+))?\s*\}\}")


def dig(obj: Any, path: str | None) -> Any:
    for part in (path or "").split("."):
        if not part:
            continue
        obj = obj.get(part) if isinstance(obj, dict) else (obj[int(part)] if isinstance(obj, list) and part.isdigit() else None)
    return obj


def render(template: Any, item: dict[str, Any]) -> Any:
    """{{input.goal}} · {{expected}} 치환. 문자열 전체가 변수 하나면 원래 타입을 유지한다."""
    if isinstance(template, dict):
        return {k: render(v, item) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, item) for v in template]
    if not isinstance(template, str):
        return template
    src = {"input": item.get("input"), "expected": item.get("expected_output")}
    whole = _VAR.fullmatch(template.strip())
    if whole:
        return dig(src[whole.group(1)], whole.group(2))

    def sub(m: re.Match) -> str:
        v = dig(src[m.group(1)], m.group(2))
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return _VAR.sub(sub, template)
