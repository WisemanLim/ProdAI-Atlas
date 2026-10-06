"""BL-HARN 하네스 외부화 (FR-BL-HARN-01~06) · BL-COMP 계산 (FR-BL-COMP-03·06).

- 아티팩트(prompt · harness · rules) 는 불변 버전 + 라벨(production·staging). production 라벨 이동은 게시 게이트(회귀 스위트) 통과 필요.
- 보정 플래그는 BL DB 가 정본, flagd 파일로 게시(OFREP 로 에이전트가 읽음). 메타데이터(도입 이유·도입 모델·제거 조건) 필수.
- ablation: 플래그별 끈 변형 + 전부 끈 변형을 같은 스위트로 돌려 removable/keep 권고.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from units.bl import clients, expr

KINDS = {"prompt", "harness", "rules"}
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def validate_body(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    if kind == "prompt":
        if not isinstance(body.get("template"), str) or not body["template"].strip():
            raise ValueError("prompt.template 필수")
        declared = set((body.get("variables") or {}).keys())
        used = set(_VAR.findall(body["template"]))
        if declared and not used <= declared:
            raise ValueError(f"선언되지 않은 변수: {sorted(used - declared)}")
        return {"template": body["template"], "variables": body.get("variables") or {}, "model_config": body.get("model_config") or {}}
    if kind == "harness":
        h = body.get("manifest") if "manifest" in body else body
        if isinstance(h, str):
            h = yaml.safe_load(h)
        loop = (h or {}).get("loop") or {}
        if "max_turns" in loop and not (isinstance(loop["max_turns"], int) and 1 <= loop["max_turns"] <= 200):
            raise ValueError("loop.max_turns 1~200")
        if "tools" in h and not (isinstance(h["tools"], list) and all(isinstance(t, str) for t in h["tools"])):
            raise ValueError("tools 는 문자열 목록")
        return {"manifest": h}
    if kind == "rules":
        doc = body.get("yaml")
        rs = yaml.safe_load(doc) if isinstance(doc, str) else body.get("rules")
        rules = rs.get("rules") if isinstance(rs, dict) else rs
        if not isinstance(rules, list) or not rules:
            raise ValueError("rules: [{id, when, action}] 필요")
        for r in rules:
            if not r.get("id") or not r.get("when"):
                raise ValueError("규칙마다 id·when 필수")
            expr.compile_expr(r["when"])
        return {"rules": rules}
    raise ValueError(kind)


def evaluate_rules(rules: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    """도메인 규칙 평가 (FR-BL-HARN-02) — 참인 규칙·행동 목록. 알 수 없는 변수는 오류로 돌려준다."""
    out, errors = [], []
    for r in rules:
        try:
            if expr.evaluate(r["when"], facts):
                out.append({"id": r["id"], "action": r.get("action", "flag"), "message": r.get("message", "")})
        except expr.ExprError as exc:
            errors.append({"id": r["id"], "error": str(exc)})
    deny = any(m["action"] in ("deny", "block") for m in out)
    approve = any(m["action"] in ("require_approval", "approve") for m in out)
    return {"matched": out, "errors": errors, "decision": "deny" if deny else "require_approval" if approve else "allow"}


# ---- 보정 플래그 → flagd 파일 ----

def flags_path() -> Path:
    return Path(os.environ.get("ATLAS_FLAGS_FILE", "/flags/flags.json"))


def write_flagd(rows: list[Any]) -> None:
    """flagd 스키마로 원자적 기록 (임시 파일 → rename, flagd 가 감시)."""
    doc = {"$schema": "https://flagd.dev/schema/v0/flags.json", "flags": {
        r.key: {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "on" if r.enabled else "off",
                "metadata": {"reason": r.reason, "introducedFor": r.introduced_for, "removalCondition": r.removal_condition}} for r in rows}}
    p = flags_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        os.replace(tmp, p)
    except OSError:
        pass   # 파일 경로가 없으면(시험·단독) DB 만 — 에이전트는 default


# ---- 게시 게이트 · ablation 용 스위트 조립 ----

def with_metadata(suite: dict[str, Any], md: dict[str, Any], tag: str) -> dict[str, Any]:
    s = copy.deepcopy(suite)
    for t in s["targets"]:
        if t.get("type") == "agent":
            t["metadata"] = {**(t.get("metadata") or {}), **md}
    s["name"] = f"{s['name']}.{tag}"[:128]
    return s


def summary_of(run: dict[str, Any]) -> dict[str, float]:
    s = run["summary"]
    return {"pass_rate": s["pass_rate"], "pass_hat_k": s["pass_hat_k"], "cost_usd": s["cost_usd"],
            "p95_ms": max((v["p95_latency_ms"] for v in s["per_target"].values()), default=0), "errors": s["errors"]}


async def ablation(suite: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """제거 실험 (FR-BL-HARN-05): 기준(모두 켬) · 각 1개 끔 · 전부 끔 → 품질이 유의하게 나빠지지 않고 비용·지연이 나빠지지 않으면 removable."""
    if not keys or not all(k.startswith("harness.") for k in keys):
        raise ValueError("flags 는 harness.* 키 목록")
    variants = [("baseline", {k: True for k in keys})] + [(f"off:{k}", {**{x: True for x in keys}, k: False}) for k in keys]
    if len(keys) > 1:
        variants.append(("all-off", {k: False for k in keys}))
    runs = {}
    for name, fl in variants:
        runs[name] = await clients.ev_run(with_metadata(suite, {"atlas.flags": fl}, name.replace(":", "-")))
    base = summary_of(runs["baseline"])
    out = []
    for name, _ in variants[1:]:
        m = summary_of(runs[name])
        d = await clients.ev_diff(runs["baseline"]["run_id"], runs[name]["run_id"])
        worse = d["delta_pass_rate"] < -0.01 and (d["significant"] or d["matrix"]["pass_fail"] >= 3 or m["pass_rate"] < base["pass_rate"] - 0.2)
        cheaper = m["cost_usd"] <= base["cost_usd"] * 1.001 and m["p95_ms"] <= max(base["p95_ms"] * 1.1, base["p95_ms"] + 50)
        out.append({"variant": name, "metrics": m, "delta_pass_rate": d["delta_pass_rate"], "mcnemar_p": d["mcnemar_p"], "pass_to_fail": d["matrix"]["pass_fail"],
                    "recommendation": "removable" if (not worse and cheaper) else "keep", "run_id": runs[name]["run_id"]})
    return {"baseline": {"metrics": base, "run_id": runs["baseline"]["run_id"]}, "variants": out,
            "removable": [v["variant"].split(":", 1)[1] for v in out if v["variant"].startswith("off:") and v["recommendation"] == "removable"]}


# ---- BL-COMP ----

def verify(v: dict[str, Any], text: str) -> bool:
    t = v.get("type")
    if t == "regex":
        return re.search(v["pattern"], text) is not None
    if t == "contains":
        return str(v["value"]) in text
    if t == "json_schema":
        import jsonschema
        try:
            jsonschema.validate(json.loads(text), v["schema"])
            return True
        except (ValueError, jsonschema.ValidationError):
            return False
    raise ValueError("verifier.type = regex | contains | json_schema")


async def best_of_n(model: str, messages: list[dict[str, Any]], n: int, verifier: dict[str, Any], max_usd: float = 0.05,
                    temperature: float = 0.8) -> dict[str, Any]:
    """반복 샘플링 + 검증기 (FR-BL-COMP-03): 첫 통과에서 조기 종료, 비용 상한 comp.max_samples_usd."""
    if not 1 <= n <= 16:
        raise ValueError("n 1~16")
    verify(verifier, "")   # 형식 검사 (잘못된 type 이면 여기서 오류)
    tried, spent = [], 0.0
    for i in range(n):
        if spent >= max_usd:
            return {"status": "budget_exhausted", "samples": tried, "spent_usd": round(spent, 8), "answer": None}
        body, hdr = await clients.chat({"model": model, "messages": messages, "temperature": temperature, "seed": i}, "agent_id:bl,unit_id:BL-COMP")
        text = body["choices"][0]["message"].get("content") or ""
        spent += float(hdr.get("x-atlas-cost-usd", "0") or 0)
        ok = verify(verifier, text)
        tried.append({"i": i, "passed": ok, "output": text[:500]})
        if ok:
            return {"status": "verified", "chosen": i, "answer": text, "samples": tried, "spent_usd": round(spent, 8)}
    return {"status": "no_verified_sample", "answer": None, "samples": tried, "spent_usd": round(spent, 8)}


def pareto(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """비용↓·품질↑ 파레토 프런티어 (다른 점보다 비싸지 않으면서 품질도 낮지 않은 점에 지배되지 않는 점)."""
    front = []
    for p in points:
        dominated = any(q is not p and q["cost_usd"] <= p["cost_usd"] and q["pass_rate"] >= p["pass_rate"]
                        and (q["cost_usd"] < p["cost_usd"] or q["pass_rate"] > p["pass_rate"]) for q in points)
        if not dominated:
            front.append(p)
    return sorted(front, key=lambda x: x["cost_usd"])


async def curve(suite: dict[str, Any], efforts: list[str], tolerance_pp: float = 1.0) -> dict[str, Any]:
    """품질-계산 곡선 (FR-BL-COMP-06): effort 별로 같은 스위트 실행 → 파레토 → 최고 품질과 tolerance 안의 최저 비용 설정 추천."""
    pts = []
    for e in efforts:
        s = copy.deepcopy(suite)
        for t in s["targets"]:
            if t.get("type") == "prompt":
                t["effort"] = e
        s["name"] = f"{s['name']}.effort-{e}"[:128]
        r = await clients.ev_run(s)
        pts.append({"effort": e, "run_id": r["run_id"], **summary_of(r)})
    best = max(p["pass_rate"] for p in pts)
    ok = [p for p in pts if p["pass_rate"] >= best - tolerance_pp / 100]
    rec = min(ok, key=lambda p: p["cost_usd"])
    return {"points": pts, "frontier": pareto(pts), "recommended": {"effort": rec["effort"], "pass_rate": rec["pass_rate"], "cost_usd": rec["cost_usd"],
                                                                     "reason": f"최고 품질 {best} 의 {tolerance_pp}pp 안에서 최저 비용"}}
