"""BL-MODEL 모델 교체 자동화 (FR-BL-MODEL-01~08): 카탈로그 → 후보 0% 등록 → 기준·후보 회귀 평가(pass^k) → 채택 규칙 →
카나리(5→25→50→100%, 고정 배정·SLO 자동 롤백) → 별칭 전환(기존 모델은 폴백) → 감사·모델 카드. 수명종료 스캔은 대체 후보를 자동 등록한다."""
from __future__ import annotations

import copy
import logging
from decimal import Decimal
from typing import Any

from units.bl import clients, expr

log = logging.getLogger("atlas.bl")

DEFAULT_RULES = [
    {"when": "cand_pass_hat_k >= base_pass_hat_k - 0.01 and cand_cost_per_case <= base_cost_per_case * 1.2 "
             "and cand_p95_ms <= max(base_p95_ms * 1.3, base_p95_ms + 50)", "decision": "promote"},
    {"when": "cand_pass_hat_k > base_pass_hat_k + 0.05", "decision": "review-cost"},
    {"when": "True", "decision": "hold"},
]
DEFAULT_STAGES = [5, 25, 50, 100]
DEFAULT_SLO = {"max_error_rate": 0.05, "max_p95_ratio": 1.3, "p95_slack_ms": 50, "max_cost_ratio": 1.2}


def decide(rules: list[dict[str, str]], m: dict[str, float], significant: bool) -> tuple[str, str]:
    """첫 번째로 참인 규칙의 결정. 품질 향상을 근거로 한 결정(review-cost)이 유의하지 않으면 insufficient-evidence (BL-MODEL-04 · EV-SCORE-03)."""
    for r in rules:
        if expr.evaluate(r["when"], m):
            d = r["decision"]
            if d == "review-cost" and not significant:
                return "insufficient-evidence", f"{r['when']} 이지만 차이가 유의하지 않음"
            return d, r["when"]
    return "hold", "일치 규칙 없음"


def base_of(entry: dict[str, Any]) -> dict[str, Any]:
    return max(entry["deployments"], key=lambda d: d.get("weight", 0))


def per_case(summary: dict[str, Any], tid: str) -> dict[str, float]:
    t = summary["per_target"][tid]
    return {"pass_hat_k": float(t["pass_hat_k"]), "pass_rate": float(t["pass_rate"]),
            "cost_per_case": float(t["cost_usd"]) / max(1, int(t["results"])), "p95_ms": float(t["p95_latency_ms"])}


def retarget(suite: dict[str, Any], alias: str, tmp: str, tid: str, k: int) -> dict[str, Any]:
    s = copy.deepcopy(suite)
    targets = [t for t in s["targets"] if t.get("type") == "prompt" and t.get("model") == alias]
    if not targets:
        raise ValueError(f"스위트 {s.get('name')} 에 alias {alias} 를 쓰는 prompt 타깃이 없음")
    t = targets[0]
    t["model"], t["id"] = tmp, tid
    s["targets"], s["epochs"] = [t], k
    s["name"] = f"{s['name']}.{tid}"
    s["thresholds"] = {"pass_rate": 0.0}   # 판정은 채택 규칙이 한다 (여기선 측정만)
    s.pop("baseline_run_id", None)
    return s


class Lifecycle:
    def __init__(self, store, publish, actor: str = "bl"):
        self.store, self.publish, self.actor = store, publish, actor

    async def event(self, subject: str, data: dict[str, Any]) -> None:
        await self.publish(subject, data)

    async def register(self, model: str, alias: str, suites: list[dict[str, Any]], k: int = 4, rules: list[dict] | None = None,
                       note: str = "", by: str = "bl") -> Any:
        cat = {m["model"]: m for m in await clients.catalog()}
        if model not in cat:
            raise ValueError(f"카탈로그에 없는 모델 {model}")
        cur = await clients.alias(alias)
        base = base_of(cur)
        if base["model"] == model:
            raise ValueError("후보가 현재 모델과 같음")
        for r in rules or []:
            expr.compile_expr(r["when"])
        deps = [{**d, "weight": d.get("weight", 1)} for d in cur["deployments"] if d["model"] != model]
        deps.append({"provider": cat[model]["provider"], "model": model, "weight": 0})   # 트래픽 0% (BL-MODEL-03)
        row = self.store.add_candidate(alias=alias, base_model=base["model"], cand_model=model, provider=cat[model]["provider"],
                                       status="registered", decision=None, alias_version_before=int(cur.get("version", 0)),
                                       detail={"suites": suites, "k": k, "rules": rules or DEFAULT_RULES, "note": note,
                                               "base_provider": base["provider"]})
        ent = await clients.put_alias(alias, deps, cur.get("fallbacks"), note=f"candidate {row.id} {model} 0%", by=by)
        self.store.update_candidate(row.id, detail={**row.detail, "registered_alias_version": ent["version"]})
        self.store.audit(by, "candidate.register", f"alias:{alias}", {"candidate": row.id, "model": model, "alias_version": ent["version"]})
        await self.event("plt.bl.model.candidate_added", {"candidate": row.id, "alias": alias, "base_model": base["model"], "model": model, "suites": len(suites)})
        return self.store.candidate(row.id)

    async def evaluate(self, cid: str) -> Any:
        c = self.store.candidate(cid)
        d = dict(c.detail)
        self.store.update_candidate(cid, status="evaluating")
        tb, tc = f"bl-{cid[:8]}-base", f"bl-{cid[:8]}-cand"
        try:
            await clients.put_alias(tb, [{"provider": d.get("base_provider", c.provider), "model": c.base_model, "weight": 1}], note=f"평가용 기준 {cid}")
            await clients.put_alias(tc, [{"provider": c.provider, "model": c.cand_model, "weight": 1}], note=f"평가용 후보 {cid}")
            runs, metrics, sig = [], [], []
            for s in d["suites"]:
                rb = await clients.ev_run(retarget(s, c.alias, tb, "base", d["k"]))
                rc = await clients.ev_run(retarget(s, c.alias, tc, "cand", d["k"]))
                diff = await clients.ev_diff(rb["run_id"], rc["run_id"])
                mb, mc = per_case(rb["summary"], "base"), per_case(rc["summary"], "cand")
                runs.append({"suite": s["name"], "base_run": rb["run_id"], "cand_run": rc["run_id"], "base": mb, "cand": mc,
                             "diff": {k: diff[k] for k in ("matrix", "delta_pass_rate", "ci95", "mcnemar_p", "significant")}})
                metrics.append((mb, mc))
                sig.append(bool(diff["significant"]))
            n = len(metrics)
            m = {f"{side}_{k}": round(sum(x[i][k] for x in metrics) / n, 8) for i, side in ((0, "base"), (1, "cand"))
                 for k in ("pass_hat_k", "pass_rate", "cost_per_case", "p95_ms")}
            decision, why = decide(d["rules"], m, all(sig))
            d.update(runs=runs, metrics=m, significant=all(sig), reason=why)
            row = self.store.update_candidate(cid, status="decided", decision=decision, detail=d)
            self.store.audit(self.actor, "candidate.decide", f"alias:{c.alias}", {"candidate": cid, "decision": decision, "metrics": m})
            await self.event("plt.bl.model.decided", {"candidate": cid, "alias": c.alias, "model": c.cand_model, "decision": decision, "metrics": m})
            return row
        except Exception as exc:
            d["error"] = str(exc)[:500]
            self.store.update_candidate(cid, status="error", detail=d)
            raise
        finally:
            for t in (tb, tc):
                try:
                    await clients.delete_alias(t)
                except clients.GatewayError:
                    pass

    async def _weights(self, c, pct: int, note: str) -> dict[str, Any]:
        cur = await clients.alias(c.alias)
        deps = []
        for dpl in cur["deployments"]:
            if dpl["model"] == c.cand_model:
                deps.append({**dpl, "weight": pct})
            elif dpl["model"] == c.base_model:
                deps.append({**dpl, "weight": 100 - pct})
        return await clients.put_alias(c.alias, deps, cur.get("fallbacks"), note=note)

    async def canary_start(self, cid: str, stages: list[int] | None = None, min_samples: int = 50, slo: dict | None = None, force: bool = False) -> Any:
        c = self.store.candidate(cid)
        if c.status != "decided" or (c.decision != "promote" and not force):
            raise ValueError(f"카나리는 promote 판정 후보만 (현재 {c.status}/{c.decision}) — 수동 진행은 force")
        stages = stages or DEFAULT_STAGES
        if stages != sorted(stages) or stages[-1] != 100 or stages[0] <= 0:
            raise ValueError("stages 는 오름차순·마지막 100")
        ent = await self._weights(c, stages[0], f"canary {cid} {stages[0]}%")
        can = {"stages": stages, "stage": 0, "min_samples": min_samples, "slo": {**DEFAULT_SLO, **(slo or {})}, "alias_version": ent["version"], "log": []}
        row = self.store.update_candidate(cid, status="canary", detail={**c.detail, "canary": can})
        await self.event("plt.bl.model.canary_stage", {"candidate": cid, "alias": c.alias, "pct": stages[0]})
        return row

    async def canary_advance(self, cid: str) -> dict[str, Any]:
        """현재 단계 표본·SLO 를 보고 다음 단계 / 승격 / 자동 롤백 (BL-MODEL-05)."""
        c = self.store.candidate(cid)
        if c.status != "canary":
            raise ValueError(f"카나리 진행 중 아님 ({c.status})")
        can = dict(c.detail["canary"])
        st = {s["model"]: s for s in await clients.alias_stats(c.alias, can["alias_version"])}
        cand, base = st.get(c.cand_model, {}), st.get(c.base_model, {})
        pct = can["stages"][can["stage"]]
        if cand.get("requests", 0) < can["min_samples"]:
            return {"candidate": cid, "action": "wait", "pct": pct, "samples": cand.get("requests", 0), "need": can["min_samples"]}
        slo, bad = can["slo"], []
        if cand.get("error_rate", 0) > slo["max_error_rate"]:
            bad.append(f"error_rate {cand['error_rate']:.3f} > {slo['max_error_rate']}")
        if base.get("requests"):
            if cand.get("p95_ms", 0) > max(base.get("p95_ms", 0) * slo["max_p95_ratio"], base.get("p95_ms", 0) + slo["p95_slack_ms"]):
                bad.append(f"p95 {cand['p95_ms']}ms > 기준 {base['p95_ms']}ms × {slo['max_p95_ratio']}")
            if base.get("cost_per_request_usd") and cand.get("cost_per_request_usd", 0) > base["cost_per_request_usd"] * slo["max_cost_ratio"]:
                bad.append(f"요청당 비용 {cand['cost_per_request_usd']:.8f} > 기준 × {slo['max_cost_ratio']}")
        stats = {"cand": cand, "base": base}
        if bad:
            await self.rollback(cid, reason="SLO 위반: " + "; ".join(bad), stats=stats)
            return {"candidate": cid, "action": "rolled_back", "pct": pct, "reasons": bad, "stats": stats}
        can["log"].append({"pct": pct, "stats": stats})
        if can["stage"] + 1 >= len(can["stages"]) or can["stages"][can["stage"] + 1] >= 100:
            self.store.update_candidate(cid, detail={**c.detail, "canary": can})
            await self.promote(cid)
            return {"candidate": cid, "action": "promoted", "pct": 100, "stats": stats}
        can["stage"] += 1
        nxt = can["stages"][can["stage"]]
        ent = await self._weights(c, nxt, f"canary {cid} {nxt}%")
        can["alias_version"] = ent["version"]
        self.store.update_candidate(cid, detail={**c.detail, "canary": can})
        await self.event("plt.bl.model.canary_stage", {"candidate": cid, "alias": c.alias, "pct": nxt})
        return {"candidate": cid, "action": "advanced", "pct": nxt, "stats": stats}

    async def promote(self, cid: str, by: str | None = None) -> Any:
        """별칭 전환 (BL-MODEL-06): alias → 후보 100%, 기존 모델은 `<alias>-prev` 폴백으로 앞에 둔다."""
        c = self.store.candidate(cid)
        cur = await clients.alias(c.alias)
        prev = f"{c.alias}-prev"
        base_dep = next((d for d in cur["deployments"] if d["model"] == c.base_model), {"provider": c.provider, "model": c.base_model})
        await clients.put_alias(prev, [{**base_dep, "weight": 1}], note=f"{c.alias} 이전 모델 (폴백)")
        fb = [prev] + [f for f in cur.get("fallbacks") or [] if f != prev]
        cand_dep = next((d for d in cur["deployments"] if d["model"] == c.cand_model), {"provider": c.provider, "model": c.cand_model})
        ent = await clients.put_alias(c.alias, [{**cand_dep, "weight": 100}], fb, note=f"promoted {cid}", by=by or self.actor)
        row = self.store.update_candidate(cid, status="promoted", detail={**c.detail, "promoted_alias_version": ent["version"]})
        self.store.audit(by or self.actor, "candidate.promote", f"alias:{c.alias}", {"candidate": cid, "from": c.base_model, "to": c.cand_model,
                                                                                   "alias_version": ent["version"], "fallbacks": fb})
        await self.event("plt.bl.model.promoted", {"candidate": cid, "alias": c.alias, "from": c.base_model, "to": c.cand_model, "fallbacks": fb})
        return row

    async def rollback(self, cid: str, reason: str = "manual", stats: dict | None = None, by: str | None = None) -> Any:
        """원클릭 롤백: 후보 등록 전 alias 버전으로 (0 = 설정 파일)."""
        c = self.store.candidate(cid)
        if c.alias_version_before == 0:
            await clients.delete_alias(c.alias)
        else:
            await clients.rollback_alias(c.alias, c.alias_version_before, by=by or self.actor)
        row = self.store.update_candidate(cid, status="rolled_back", detail={**c.detail, "rollback": {"reason": reason, "stats": stats}})
        self.store.audit(by or self.actor, "candidate.rollback", f"alias:{c.alias}", {"candidate": cid, "reason": reason})
        await self.event("plt.bl.model.rolled_back", {"candidate": cid, "alias": c.alias, "model": c.cand_model, "reason": reason})
        return row


def price(entry: dict[str, Any]) -> Decimal:
    p = entry.get("prices") or {}
    return Decimal(p.get("input", "999")) + Decimal(p.get("output", "999"))


def replacement(cat: list[dict[str, Any]], old: dict[str, Any], days: int) -> dict[str, Any] | None:
    """대체 후보: 같은 공급자·폐기 예정 아님·모달리티 포함·도구/추론 능력 이상·컨텍스트 이상 중 최저가 (BL-MODEL-07)."""
    ok = []
    for m in cat:
        if m["model"] == old["model"] or m.get("provider") != old.get("provider"):
            continue
        if m.get("deprecation_in") is not None and m["deprecation_in"] <= days:
            continue
        if not set(old.get("modalities") or []) - {"image"} <= set(m.get("modalities") or []):
            continue
        if (old.get("tools") and not m.get("tools")) or m.get("context_window", 0) < old.get("context_window", 0) // 2:
            continue
        if m["model"].startswith(("mock-judge", "mock-503", "mock-429", "mock-planner")):
            continue
        ok.append(m)
    return min(ok, key=price) if ok else None


def model_card(model: str, cat: dict[str, Any], aliases: list[dict[str, Any]], cands: list[Any], audits: list[Any]) -> str:
    """모델 카드 (BL-MODEL-08) — 용도·제한·평가 이력·승격 근거·승인자. EU AI Act·AI 기본법 문서화 export."""
    m = cat.get(model, {})
    using = [a["name"] for a in aliases if any(d["model"] == model and d.get("weight", 0) > 0 for d in a["deployments"])]
    lines = [f"# 모델 카드 — {model}", "", f"- 공급자: {m.get('provider', '?')} · 컨텍스트 {m.get('context_window', '?')} · 최대 출력 {m.get('max_output', '?')}",
             f"- 모달리티: {', '.join(m.get('modalities') or [])} · 도구 {m.get('tools')} · 추론 {m.get('reasoning', False)} · 지역 {', '.join(m.get('regions') or [])}",
             f"- 단가(USD/1M): {m.get('prices')}", f"- 폐기 예정일: {m.get('deprecation_date') or '없음'}", f"- 사용 중인 alias: {', '.join(using) or '없음'}", "",
             "## 평가·승격 이력", "", "| 후보 | alias | 기준 → 후보 | 판정 | 상태 | pass^k 기준/후보 | 과업당 비용 기준/후보 |", "|---|---|---|---|---|---|---|"]
    for c in cands:
        if model not in (c.cand_model, c.base_model):
            continue
        mt = (c.detail or {}).get("metrics") or {}
        lines.append(f"| {c.id[:8]} | {c.alias} | {c.base_model} → {c.cand_model} | {c.decision or '-'} | {c.status} | "
                     f"{mt.get('base_pass_hat_k', '-')}/{mt.get('cand_pass_hat_k', '-')} | {mt.get('base_cost_per_case', '-')}/{mt.get('cand_cost_per_case', '-')} |")
    lines += ["", "## 승인·변경 기록", ""]
    lines += [f"- {a.ts.isoformat()} · {a.actor} · {a.action} · {a.target}" for a in audits] or ["- 없음"]
    lines += ["", "## 제한·주의", "", "- 평가 결과는 등록된 회귀 스위트 범위에 한정된다. 스위트 밖 과제의 품질은 보장하지 않는다."]
    return "\n".join(lines) + "\n"
