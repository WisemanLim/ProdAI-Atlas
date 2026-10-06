"""GR-IO 검사기 — jailbreak · pii · safety · output_format · spotlight · tool_policy. 모두 같은 인터페이스(Stage → StageResult)."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import jsonschema

from units import gr_jb, gr_pii, gr_pol


@dataclass
class StageResult:
    checker: str
    flagged: bool
    score: float = 0.0
    categories: list[str] = field(default_factory=list)
    transformed: str | None = None
    detail: str = ""
    approval: bool = False          # tool_policy: require_approval
    decisive: bool = True           # cascade 확정 여부


# ---- 유해성 분류 공급자 추상화 (FR-GR-IO-04) — MLCommons 위험 분류 코드 사용 ----
@dataclass
class SafetyResult:
    label: str                       # safe | unsafe
    categories: list[str]
    scores: dict[str, float]


class SafetyProvider(Protocol):
    name: str

    def classify(self, text: str, role: str) -> SafetyResult: ...


class KeywordSafetyProvider:
    """결정적 키워드·패턴 공급자 (tier 1). Llama Guard·ShieldGemma·상용 API 는 같은 인터페이스로 교체."""

    name = "keyword-v1"
    RULES: ClassVar[dict[str, re.Pattern[str]]] = {
        "S1": re.compile(r"(살인|청부\s*살인|사람을\s*죽이).{0,12}(방법|의뢰|하는\s*법)|how\s+to\s+(murder|kill\s+(someone|a\s+person))", re.IGNORECASE),
        "S2": re.compile(r"(필로폰|코카인|헤로인|마약|meth(amphetamine)?).{0,12}(제조|만드는|합성|cook|synthesi[sz]e)", re.IGNORECASE),
        "S9": re.compile(r"(폭탄|폭발물|사제\s*폭탄|신경\s*가스|사린|bomb|explosive|nerve\s+agent|sarin).{0,15}(만드|제조|제작|합성|조립|make|build|synthesi[sz]e|assemble)", re.IGNORECASE),
        "S10": re.compile(r"(인종|민족|종교)\s*(청소|말살)|ethnic\s+cleansing\s+(is|was)\s+(good|justified)", re.IGNORECASE),
        "S11": re.compile(r"(자살|자해).{0,10}(방법|하는\s*법|도구)|how\s+to\s+(kill\s+myself|commit\s+suicide|self[- ]harm)", re.IGNORECASE),
    }

    def classify(self, text: str, role: str) -> SafetyResult:
        cats = [c for c, rx in self.RULES.items() if rx.search(text)]
        return SafetyResult("unsafe" if cats else "safe", cats, {c: 1.0 for c in cats})


SAFETY_PROVIDERS: dict[str, SafetyProvider] = {"keyword-v1": KeywordSafetyProvider()}

# ---- 하위 시스템 안전 (FR-GR-IO-06) ----
HTML_DANGER = re.compile(r"<\s*script|\son\w+\s*=|javascript:|<\s*iframe|<\s*object", re.IGNORECASE)
SQL_DANGER = re.compile(r";\s*(drop|truncate|alter|create|grant)\b|\bdrop\s+table\b|\bdelete\s+from\s+\w+\s*(;|$)|--|/\*|\bunion\s+select\b", re.IGNORECASE)
SHELL_ALLOW = {"ls", "cat", "echo", "grep", "wc", "head", "tail", "pwd", "date"}
SHELL_DANGER = re.compile(r"[;&|`$<>]|\brm\s+-rf\b|\bsudo\b|\bcurl\b.*\|\s*(sh|bash)")


def check_output_format(text: str, schema: dict | None, sink: str | None, mode: str) -> StageResult:
    if schema:
        import json
        try:
            jsonschema.validate(json.loads(text), schema)
        except (ValueError, jsonschema.ValidationError) as exc:
            return StageResult("output_format", True, 1.0, ["schema"], detail=getattr(exc, "message", str(exc))[:200])
    if sink == "html" and HTML_DANGER.search(text):
        return StageResult("output_format", True, 1.0, ["html_injection"], transformed=html.escape(text) if mode == "transform" else None)
    if sink == "sql" and SQL_DANGER.search(text):
        return StageResult("output_format", True, 1.0, ["sql_injection"])
    if sink == "shell":
        cmd = text.strip().split()
        if SHELL_DANGER.search(text) or not cmd or cmd[0] not in SHELL_ALLOW:
            return StageResult("output_format", True, 1.0, ["shell_command"], detail=cmd[0] if cmd else "")
    return StageResult("output_format", False)


def session_key(secret: str, tenant: str, session: str) -> bytes:
    return hashlib.sha256(f"{secret}|{tenant}|{session}".encode()).digest()


def run_checker(stage: dict[str, Any], text: str, ctx: dict[str, Any]) -> StageResult:
    name = stage["checker"]
    if name == "jailbreak":
        th = float(stage.get("threshold", 0.6))
        if ctx.get("boundary") in ("tool_result", "retrieval"):        # 비신뢰 경계 = 간접 인젝션 스캔 (GR-JB-02)
            ir = gr_jb.scan_indirect(text, th)
            ctx.setdefault("indirect", {})[ctx.get("_index", 0)] = ir.flagged_chunks
            return StageResult(name, bool(ir.flagged_chunks) or (bool(ir.hidden) and ir.score >= th) or ir.score >= th, ir.score, ir.categories)
        r = gr_jb.classify(text)
        return StageResult(name, r.score >= th, r.score, r.matches, decisive=r.decisive())
    if name == "pii":
        pii_cfg = ctx.get("pii", {})
        found = gr_pii.analyze(text, pii_cfg.get("entities"), float(pii_cfg.get("threshold", 0.5)))
        if not found:
            return StageResult(name, False)
        out = gr_pii.anonymize(text, found, operators=pii_cfg.get("operators"), salt=ctx["salt"], pseudo=ctx["pseudo"])
        return StageResult(name, True, max(f.score for f in found), sorted({f.entity_type for f in found}), transformed=out)
    if name == "safety":
        provider = SAFETY_PROVIDERS[stage.get("provider", "keyword-v1")]
        r = provider.classify(text, ctx.get("role", "user"))
        return StageResult(name, r.label == "unsafe", max(r.scores.values(), default=0.0), r.categories)
    if name == "output_format":
        return check_output_format(text, ctx.get("response_schema"), ctx.get("sink"), stage["mode"])
    if name == "spotlight":
        clean = gr_jb.quarantine(gr_jb.sanitize(text), (ctx.get("indirect") or {}).get(ctx.get("_index", 0), []))   # 숨김 제거·의심 청크 격리
        out, rule = gr_jb.spotlight(clean, stage.get("spotlight", "delimit"))
        ctx["spotlight_rule"] = rule
        return StageResult(name, True, 0.0, ["spotlight"], transformed=out)
    if name == "tool_policy":
        d = gr_pol.decide_tool(ctx["bundle"], ctx.get("tool") or {}, ctx.get("args") or {}, ctx.get("principal"), ctx.get("provenance"))
        return StageResult(name, d.decision == gr_pol.DENY, 1.0 if d.decision != gr_pol.ALLOW else 0.0, [d.rule] if d.rule else [],
                           detail="; ".join(d.reasons), approval=d.decision == gr_pol.REQUIRE_APPROVAL)
    raise ValueError(f"알 수 없는 checker {name}")
