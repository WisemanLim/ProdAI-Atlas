"""설치 전 보안 스캔 (FR-MCP-REG-06) — 도구 설명·스키마의 tool poisoning 정적 규칙. 점수 0~100, 기본 차단 70.
LLM 판정(EV-JUDGE)·agent-scan 연동은 후속."""
from __future__ import annotations

import re
from typing import Any

RULES: list[tuple[str, int, re.Pattern[str]]] = [
    ("hidden_unicode", 40, re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\U000e0000-\U000e007f]")),
    ("instruction_tag", 35, re.compile(r"<\s*/?\s*(important|system|instructions?)\s*>|\[/?INST\]", re.IGNORECASE)),
    ("override_phrase", 35, re.compile(r"ignore (all )?(previous|prior|above)|disregard (the )?(previous|above)|이전\s*지시|지시(를|사항을)?\s*무시|무시하고", re.IGNORECASE)),
    ("secret_path", 30, re.compile(r"~/\.ssh|id_rsa|\.aws/credentials|/etc/passwd|\.env\b|api[_ -]?key|private key|비밀번호|토큰을", re.IGNORECASE)),
    ("exfiltration", 20, re.compile(r"(send|post|upload|전송)\s.{0,40}(https?://|webhook)|curl\s+-", re.IGNORECASE)),
]
SENSITIVE_PARAMS = re.compile(r"^(password|passwd|secret|token|api_?key|credential)s?$", re.IGNORECASE)


def scan_tool(tool: dict[str, Any]) -> list[dict[str, Any]]:
    text = f"{tool.get('name', '')}\n{tool.get('title') or ''}\n{tool.get('description') or ''}"
    findings = [{"rule": rule, "score": score, "match": m.group(0)[:60]}
                for rule, score, rx in RULES if (m := rx.search(text))]
    if len(tool.get("description") or "") > 2000:
        findings.append({"rule": "oversized_description", "score": 10, "match": f"{len(tool['description'])} chars"})
    for p in (tool.get("input_schema") or {}).get("properties", {}):
        if SENSITIVE_PARAMS.match(p):
            findings.append({"rule": "sensitive_param", "score": 10, "match": p})
    return findings


def scan(tools: list[dict[str, Any]]) -> dict[str, Any]:
    per_tool = {t["name"]: f for t in tools if (f := scan_tool(t))}
    score = min(100, max((sum(x["score"] for x in f) for f in per_tool.values()), default=0))
    return {"score": score, "findings": per_tool, "tools": len(tools)}
