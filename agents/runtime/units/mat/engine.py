"""MAT 판정 (FR-MAT-01·02·03·04·05·06·09·10) — 사실(facts) → 4속성 점수 · L0~L4 단계 · 개념 커버리지 · 복리 루프 · 로드맵 · 지표 · 진화 띠 · 성과."""
from __future__ import annotations

from typing import Any

from units.bl import expr

PHASES = {0: "Surface", 1: "Production", 2: "Production", 3: "Compound", 4: "Compound"}
STAGE_NAMES = {0: "Surface Prompts"}
ATTR_LABEL = {"reliable": "Reliable", "observable": "Observable", "cost_aware": "Cost-aware", "safe": "Safe"}


def judge(when: str, facts: dict[str, Any]) -> str:
    """pass | fail | unknown(사실 없음·형 불일치)."""
    env = {k: v for k, v in facts.items()}
    try:
        return "pass" if expr.evaluate(when, env) else "fail"
    except expr.ExprError:
        return "unknown"


def attributes(d: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for attr, checks in d["attributes"].items():
        res = [{"id": c["id"], "desc": c["desc"], "weight": c["weight"], "result": judge(c["when"], facts), "rule": c["when"]} for c in checks]
        total = sum(c["weight"] for c in res)
        got = sum(c["weight"] for c in res if c["result"] == "pass")
        out[attr] = {"score": round(100 * got / total) if total else 0, "checks": res}
    return out


def stage(d: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """게이트를 모두 충족한 연속 단계까지만 오른다 (FR-MAT-02)."""
    level, levels = 0, []
    for s in sorted(d["stages"], key=lambda x: x["level"]):
        gates = [{**g, "result": judge(g["when"], facts)} for g in s["gates"]]
        ok = all(g["result"] == "pass" for g in gates)
        levels.append({"level": s["level"], "name": s["name"], "phase": s["phase"], "passed": ok, "gates": gates})
        if ok and level == s["level"] - 1:
            level = s["level"]
    STAGE_NAMES.update({s["level"]: s["name"] for s in d["stages"]})
    nxt = next((x for x in levels if x["level"] == level + 1), None)
    return {"level": level, "name": STAGE_NAMES[level], "phase": PHASES[level], "levels": levels,
            "next": None if nxt is None else {"level": nxt["level"], "name": nxt["name"],
                                              "unmet": [{"id": g["id"], "task": g["task"], "role": g["role"], "result": g["result"]} for g in nxt["gates"] if g["result"] != "pass"]}}


def coverage(d: dict[str, Any], facts: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    """9개념(+A2A) × 4속성 (FR-MAT-03): ● = 개념 활성 + 속성 점수 70 이상, ◐ = 개념 활성, 빈칸 = 미도입."""
    gateway_on = (facts.get("ob_requests_24h") or 0) > 0
    active = {}
    for cpt in d["concepts"]:
        n = facts.get("concept_" + cpt.replace("-", "_")) or 0
        if cpt in ("04-ai-gateway", "05-inference-economics"):     # Gateway 내장 개념
            n = int(gateway_on) + (facts.get("gw_budgets") or 0 if cpt == "05-inference-economics" else 0)
        if cpt == "a2a":
            n = facts.get("mgr_a2a_signed") or 0
        active[cpt] = n
    matrix = {cpt: {a: ("●" if n and attrs[a]["score"] >= 70 else "◐" if n else "") for a in attrs} for cpt, n in active.items()}
    return {"active": active, "matrix": matrix, "adopted": sum(1 for n in active.values() if n), "total": len(active)}


def compound(facts: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    """복리 루프 가동 (FR-MAT-04): trace → 골든셋 → 회귀 → 개선 배포."""
    prom, appr = facts.get("ev_trace_promotions_30d"), facts.get("ev_trace_promoted_approved")
    trend = None
    costs = [h["facts"].get("ev_cost_per_task") for h in history if h["facts"].get("ev_cost_per_task") is not None]
    if facts.get("ev_cost_per_task") is not None and costs:
        prev = costs[-1]
        trend = "flat" if prev == 0 else ("down" if facts["ev_cost_per_task"] < prev * 0.98 else "up" if facts["ev_cost_per_task"] > prev * 1.02 else "flat")
    running = bool(prom) and bool(facts.get("ev_runs_30d")) and (facts.get("bl_promoted_regressions") or 0) == 0
    return {"trace_promotions_30d": prom, "promotions_in_regression_rate": round(appr / prom, 4) if prom else None,
            "changes_blocked_by_regression_30d": facts.get("ev_blocked_changes_30d"), "model_swap_regressions": facts.get("bl_promoted_regressions"),
            "cost_per_task_trend": trend, "loop_running": running}


def metrics(d: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**m, "value": facts.get(m["fact"])} for m in d["metrics"]]


def roadmap(st: dict[str, Any]) -> dict[str, Any]:
    """미충족 게이트 → 90일(13주) 계획 작업 카드 (FR-MAT-05). 다음 단계부터 차례로, 역할별 병렬."""
    cards, week = [], {}
    for lv in st["levels"]:
        if lv["passed"] and lv["level"] <= st["level"]:
            continue
        for g in lv["gates"]:
            if g["result"] == "pass":
                continue
            start = week.get(g["role"], 1)
            if start > 13:
                continue
            end = min(13, start + g["weeks"] - 1)
            cards.append({"title": g["task"], "gate": f"L{lv['level']}:{g['id']}", "role": g["role"], "weeks": [start, end], "level": lv["level"]})
            week[g["role"]] = end + 1
    issues = [{"title": f"[MAT L{c['level']}] {c['title']}", "labels": ["maturity", f"role:{c['role']}", f"L{c['level']}"],
               "body": f"게이트 `{c['gate']}` 충족. 계획 주차 {c['weeks'][0]}~{c['weeks'][1]}."} for c in cards]
    return {"horizon_weeks": 13, "cards": cards, "github_issues": issues}


def band(st: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    """홈 하단 진화 띠 (FR-MAT-09): Surface → Production → Compound 위 현재 위치·다음 게이트 미달 칩."""
    lv = st["level"]
    progress = round(100 * lv / 4)
    if st["next"]:
        n_gates = len(next(x for x in st["levels"] if x["level"] == st["next"]["level"])["gates"])
        progress += round(25 * (n_gates - len(st["next"]["unmet"])) / max(1, n_gates))
    return {"stage": lv, "name": st["name"], "phase": st["phase"], "progress_pct": min(100, progress),
            "score_by_attr": {ATTR_LABEL[a]: v["score"] for a, v in attrs.items()},
            "unmet_gates": [u["id"] for u in (st["next"] or {}).get("unmet", [])], "next": (st["next"] or {}).get("name")}


def outcomes(facts: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """성과 지표 (FR-MAT-10): 'Better decisions · Lower cost · Faster wins' → 과업 성공률 · 과업당 비용 · 완료까지 턴 수, 기준선(첫 스냅샷) 대비."""
    def card(name: str, key: str, better: str) -> dict[str, Any]:
        cur = facts.get(key)
        base = (baseline or {}).get(key)
        ch = round((cur - base) / base * 100, 1) if cur is not None and base not in (None, 0) else None
        good = None if ch is None else (ch >= 0 if better == "up" else ch <= 0)
        return {"name": name, "fact": key, "current": cur, "baseline": base, "change_pct": ch, "improved": good}
    return {"cards": [card("Better decisions", "ev_task_success_rate", "up"), card("Lower cost", "ev_cost_per_task", "down"),
                      card("Faster wins", "run_avg_turns", "down")], "baseline_at": (baseline or {}).get("_at")}


def assess(d: dict[str, Any], facts: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    attrs = attributes(d, facts)
    st = stage(d, facts)
    base = dict(history[0]["facts"], _at=history[0]["created_at"]) if history else None
    return {"attributes": attrs, "stage": st, "coverage": coverage(d, facts, attrs), "compound": compound(facts, history),
            "metrics": metrics(d, facts), "roadmap": roadmap(st), "band": band(st, attrs), "outcomes": outcomes(facts, base)}
