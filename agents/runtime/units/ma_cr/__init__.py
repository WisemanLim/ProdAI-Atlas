"""MA-CR Subagent B · Code Review — unified diff 의 추가 줄을 규칙으로 검토해 구조화 이슈(+SARIF 2.1.0)를 돌려준다 (FR-MA-CR-01·02·04).

읽기 전용: diff 텍스트만 읽고 저장소·파일을 수정하지 않는다. 결정적 규칙(보안·버그·성능·스타일) — LLM 리뷰는 후속.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from units.ma_common import check_spec, estimate_tokens

MANIFEST = Path(__file__).with_name("atlas-agent.json")

# (rule_id, category, severity, pattern, rationale, suggestion)
RULES = [
    ("hardcoded-secret", "security", "critical", re.compile(r"(AKIA[0-9A-Z]{16}|(?i:(password|secret|api_?key|token))\s*[:=]\s*['\"][^'\"]{6,})"),
     "코드에 자격증명이 하드코딩됨", "비밀 저장소(Manager 비밀 중개)로 옮기고 값을 회전"),
    ("eval-exec", "security", "high", re.compile(r"\b(eval|exec)\s*\("), "동적 코드 실행 — 주입 위험", "명시적 파서·허용 목록으로 대체"),
    ("sql-concat", "security", "high", re.compile(r"(?i)(select|insert|update|delete)\b.*['\"]\s*(\+|%|\.format\(|f['\"])"),
     "문자열로 조립한 SQL — 인젝션 위험", "바인딩 파라미터 사용"),
    ("shell-true", "security", "high", re.compile(r"shell\s*=\s*True"), "셸 인젝션 위험", "인자 리스트로 subprocess 호출"),
    ("bare-except", "bug", "medium", re.compile(r"except\s*:\s*$|except\s+Exception\s*:\s*pass"), "예외를 삼켜 장애가 숨는다", "구체적 예외를 잡고 로그"),
    ("mutable-default", "bug", "medium", re.compile(r"def \w+\(.*=\s*(\[\]|\{\})"), "가변 기본 인자는 호출 간 공유됨", "None 기본값 후 함수 안에서 생성"),
    ("n-plus-one", "perf", "medium", re.compile(r"for .+ in .+:\s*$"), "루프 안 쿼리·호출 가능성 — 확인 필요", "일괄 조회로 바꿀 수 있는지 검토"),
    ("debug-print", "style", "low", re.compile(r"^\s*(print\(|console\.log\()"), "디버그 출력", "로거 사용"),
    ("todo", "style", "info", re.compile(r"(TODO|FIXME|XXX)"), "남은 작업 표시", "이슈로 옮기기"),
]


def parse_diff(diff: str) -> list[dict[str, Any]]:
    """추가된 줄만 (file, line, text). hunk 헤더 @@ -a,b +c,d @@ 로 새 파일 줄 번호를 센다."""
    out, file, line = [], None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            file = raw[4:].removeprefix("b/").strip()
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif file and raw.startswith("+") and not raw.startswith("+++"):
            out.append({"file": file, "line": line, "text": raw[1:]})
            line += 1
        elif file and not raw.startswith("-"):
            line += 1
    return out


def review_lines(added: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    for a in added:
        for rid, cat, sev, pat, why, fix in RULES:
            if rid == "n-plus-one" and not re.search(r"(query|execute|fetch|get\(|requests\.|httpx\.)", a["text"] + "".join(
                    x["text"] for x in added if x["file"] == a["file"] and a["line"] < x["line"] <= a["line"] + 2)):
                continue
            if pat.search(a["text"]):
                issues.append({"file": a["file"], "line_start": a["line"], "line_end": a["line"], "severity": sev, "category": cat,
                               "rule": rid, "rationale": why, "suggestion": fix})
    files = {a["file"] for a in added}
    src = [f for f in files if f.endswith((".py", ".go", ".ts", ".js")) and "test" not in f]
    if src and not any("test" in f for f in files):
        issues.append({"file": src[0], "line_start": 1, "line_end": 1, "severity": "low", "category": "bug", "rule": "missing-tests",
                       "rationale": "코드 변경에 시험 변경이 없음", "suggestion": "변경을 덮는 시험 추가"})
    return issues


def sarif(issues: list[dict[str, Any]]) -> dict[str, Any]:
    level = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "atlas-ma-cr", "rules": [{"id": r[0], "shortDescription": {"text": r[4]}} for r in RULES]}},
                      "results": [{"ruleId": i["rule"], "level": level[i["severity"]], "message": {"text": i["rationale"]},
                                   "locations": [{"physicalLocation": {"artifactLocation": {"uri": i["file"]},
                                                                       "region": {"startLine": i["line_start"], "endLine": i["line_end"]}}}]}
                                  for i in issues]}]}


async def code_review(ctx: TaskContext) -> dict[str, Any]:
    check_spec(ctx.data)
    diff = ctx.data.get("diff") or (ctx.data.get("inputs") or {}).get("diff") or (ctx.text if ctx.text.lstrip().startswith(("diff ", "--- ", "+++ ")) else "")
    if not diff:
        raise InputRequired("검토할 unified diff(data.diff)가 필요합니다")
    added = parse_diff(diff)
    issues = review_lines(added)[: int(agent.manifest["spec"]["config"]["maxIssues"])]
    order = ["critical", "high", "medium", "low", "info"]
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in order}
    summary = f"{len({a['file'] for a in added})}개 파일 추가 {len(added)}줄 검토 — " + ", ".join(f"{s} {n}" for s, n in counts.items() if n) if issues else "이슈 없음"
    return {"kind": "review", "issues": sorted(issues, key=lambda i: (order.index(i["severity"]), i["file"], i["line_start"])),
            "summary": summary, "sarif": sarif(issues), "read_only": True, "usage": {"tokens": estimate_tokens(diff)}}


agent = AgentSpec.from_file(MANIFEST, description="MA-CR 코드 리뷰 서브에이전트 — diff 기반 구조화 이슈(ReviewResult)·SARIF 2.1.0, 읽기 전용",
                            skills=[Skill(id="ma.code_review", name="Code Review", handler=code_review,
                                          description="unified diff → ReviewResult{issues[{file, line_start, severity, category, rationale, suggestion}], summary, sarif} code review 리뷰 diff 보안 버그",
                                          tags=["review", "리뷰", "diff"], examples=["이 diff 를 보안 관점에서 검토"])])
