"""MAT 증거 수집 (FR-MAT-01·03·04·06) — 플랫폼 서비스 API 를 읽어 사실(facts)로 펼친다. 실패한 출처는 errors 에 남기고 사실은 비운다(unknown).

출처: Manager(에이전트·배포·승인함·감사·A2A) · Gateway 관리(예산·캐시·계산 정책) · OB(대시보드·SLO·경보) · EV(골든셋·실행·judge·온라인·스코어카드)
     · GR(판정) · MCP-REG(허용목록) · AL-RUN(런) · BL(후보·프롬프트)
"""
from __future__ import annotations

import asyncio
import os
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from atlas_agent_core import otel, secrets
from atlas_agent_core.a2a_client import TokenProvider

TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험용


def url(env: str, default: str) -> str:
    return (os.environ.get(env) or default).rstrip("/")


def recent(iso: str | None, days: int) -> bool:
    if not iso:
        return False
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if t.tzinfo is None:          # SQLite 저장 시각은 tz 가 없다 — UTC 로 본다
        t = t.replace(tzinfo=UTC)
    return t >= datetime.now(UTC) - timedelta(days=days)


class Collector:
    def __init__(self, spec: Any, org: str = "default"):
        self.spec, self.org, self.errors = spec, org, {}

    async def _get(self, c: httpx.AsyncClient, src: str, full: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        try:
            r = await c.get(full, params=params or None, headers=otel.inject(headers or {}))
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.errors[src] = f"{type(exc).__name__}: {exc}"[:200]
            return None

    async def manager_headers(self) -> dict[str, str]:
        mgr = self.spec.resources.get("manager_url") or os.environ.get("ATLAS_MANAGER_URL", "")
        try:
            tok = await TokenProvider(mgr, os.environ.get("ATLAS_MANAGER_TOKEN"), self.spec.resources.get("registration") or {}, TRANSPORT)()
        except httpx.HTTPError:
            tok = None
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    async def facts(self) -> dict[str, Any]:
        f: dict[str, Any] = {}
        mgr = url("ATLAS_MANAGER_URL", "http://manager:8080")
        gw, ob, ev = url("ATLAS_GATEWAY_URL", "http://gateway:8080"), url("ATLAS_OB_URL", "http://agent-ob:8000"), url("ATLAS_EV_URL", "http://agent-ev:8000")
        gr, mcp, run, bl = (url("ATLAS_GUARD_URL", "http://agent-gr:8000"), url("ATLAS_MCP_REGISTRY_URL", "http://agent-mcp-reg:8000"),
                            url("ATLAS_ALRUN_URL", "http://agent-al-run:8000"), url("ATLAS_BL_URL", "http://agent-bl:8000"))
        mh = await self.manager_headers()
        try:
            gtok = await secrets.get("gateway-admin-token", env="ATLAS_GATEWAY_ADMIN_TOKEN", dev_default="local-admin-token")
        except secrets.SecretUnavailable:
            gtok = ""
        gh = {"Authorization": f"Bearer {gtok}"}
        o = self.org
        async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
            got = await asyncio.gather(
                self._get(c, "manager.agents", f"{mgr}/api/v1/agents", mh, org=o),
                self._get(c, "manager.deployments", f"{mgr}/api/v1/deployments", mh, org=o),
                self._get(c, "manager.approvals", f"{mgr}/api/v1/approvals", mh, org=o),
                self._get(c, "manager.audit", f"{mgr}/api/v1/orgs/{o}/audit/verify", mh),
                self._get(c, "manager.registry", f"{mgr}/api/v1/a2a/registry/agents", mh, org=o, limit=200),
                self._get(c, "gateway.budgets", f"{gw}/admin/budgets", gh),
                self._get(c, "gateway.cache", f"{gw}/admin/econ/cache", gh, tenant=o),
                self._get(c, "gateway.compute", f"{gw}/admin/compute-policies", gh),
                self._get(c, "ob.dashboard", f"{ob}/v1/dashboards/llm", since=86400),
                self._get(c, "ob.slos", f"{ob}/v1/slos"),
                self._get(c, "ob.alerts", f"{ob}/v1/alerts/rules"),
                self._get(c, "ev.datasets", f"{ev}/v1/datasets"),
                self._get(c, "ev.runs", f"{ev}/v1/runs", limit=200),
                self._get(c, "ev.judges", f"{ev}/v1/judges"),
                self._get(c, "ev.online", f"{ev}/v1/online-rules"),
                self._get(c, "ev.scorecard", f"{ev}/v1/scorecard"),
                self._get(c, "gr.verdicts", f"{gr}/v1/guard/verdicts", tenant=o, limit=200),
                self._get(c, "mcp.effective", f"{mcp}/v0.1/tenants/{o}/effective"),
                self._get(c, "alrun.runs", f"{run}/runs", tenant=o, limit=200),
                self._get(c, "bl.candidates", f"{bl}/v1/candidates"),
                self._get(c, "bl.prompts", f"{bl}/v1/prompts"),
            )
            (agents, deps, approvals, audit, registry, budgets, cache, compute, dash, slos, alerts,
             datasets, runs, judges, online, card, verdicts, mcp_eff, alruns, cands, prompts_) = got
            # 골든셋 항목 출처 (승격 루프) — 데이터셋별 항목
            items = []
            for d in (datasets or {}).get("items", [])[:50]:
                r = await self._get(c, "ev.items", f"{ev}/v1/datasets/{d['name']}/items")
                items += [{**i, "_dataset": d["name"]} for i in (r or {}).get("items", [])]

        # Manager — 개념 커버리지·승인함·감사·A2A
        if agents is not None and deps is not None:
            ready = {d["agentId"] for d in deps.get("items", []) if d.get("status") == "ready"}
            for cpt in self.spec.resources["mat_def"]["concepts"]:
                f["concept_" + cpt.replace("-", "_")] = sum(1 for a in agents.get("items", []) if a.get("concept") == cpt and a["id"] in ready)
            f["mgr_agents_ready"] = len(ready)
        if approvals is not None:
            done = [a for a in approvals.get("items", []) if a.get("decidedAt")]
            f["mgr_approvals_decided"] = len(done)
            lat = [(datetime.fromisoformat(a["decidedAt"]).replace(tzinfo=None) - datetime.fromisoformat(a["createdAt"]).replace(tzinfo=None)).total_seconds()
                   for a in done]
            f["mgr_hitl_latency_s"] = round(statistics.median(lat), 2) if lat else None
        if audit is not None:
            f["mgr_audit_valid"] = bool(audit.get("valid"))
        if registry is not None:
            f["mgr_a2a_signed"] = sum(1 for a in registry.get("items", []) if a.get("trust") == "signed")
        # Gateway
        if budgets is not None:
            bs = budgets.get("data", [])
            f["gw_budgets"], f["gw_key_budgets"] = len(bs), sum(1 for b in bs if b.get("scope") == "key")
        if cache is not None:
            ratios = [k.get("hit_ratio", 0) for k in cache.get("data", []) if k.get("requests", 1)]
            f["gw_cache_hit_ratio"] = round(max(ratios), 4) if ratios else 0.0
        if compute is not None:
            f["gw_compute_policies"] = len(compute.get("data", []))
        # OB
        if dash is not None:
            s = (dash.get("summary") or [{}])[0] if dash.get("summary") else {}
            f["ob_requests_24h"], f["ob_input_tokens_24h"] = int(s.get("requests") or 0), int(s.get("input_tokens") or 0)
            f["ob_cost_24h"], f["ob_error_rate"] = float(s.get("cost_usd") or 0), float(s.get("error_rate") or 0)
        if slos is not None:
            f["ob_slos"] = len(slos.get("items", []))
        if alerts is not None:
            f["ob_alert_rules"] = len(alerts.get("items", []))
        # EV
        if datasets is not None:
            counts = [d["counts"].get("approved", 0) for d in datasets.get("items", [])]
            f["ev_approved_items_max"], f["ev_approved_items_total"] = max(counts, default=0), sum(counts)
        if runs is not None:
            done = [r for r in runs.get("items", []) if r.get("pass_rate") is not None]
            last = done[:10]
            f["ev_latest_pass_rate"] = round(sum(r["pass_rate"] for r in last) / len(last), 4) if last else None
            f["ev_runs_30d"] = sum(1 for r in done if recent(r.get("created_at"), 30))
            f["ev_gate_fail_30d"] = sum(1 for r in done if r.get("gate") == "fail" and recent(r.get("created_at"), 30))
        if judges is not None:
            f["ev_judges_trusted"] = sum(1 for j in judges.get("items", []) if (j.get("calibration") or {}).get("trusted"))
        if online is not None:
            f["ev_online_rules"] = sum(1 for r in online.get("items", []) if r.get("enabled") or r.get("last_run_at"))
        if card is not None:
            cs = card.get("items", [])
            f["ev_task_success_rate"] = round(sum(x["task_success_rate"] or 0 for x in cs) / len(cs), 4) if cs else None
            f["ev_cost_per_task"] = round(sum(x["cost_per_task_usd"] or 0 for x in cs) / len(cs), 8) if cs else None
        if datasets is not None:
            promoted = [i for i in items if i.get("source") == "prod-trace"]
            f["ev_trace_promotions_30d"] = len(promoted)
            f["ev_trace_promoted_approved"] = sum(1 for i in promoted if i.get("status") == "approved")
        # GR · MCP · AL-RUN · BL
        if verdicts is not None:
            vs = verdicts.get("items", [])
            f["gr_verdicts"], f["gr_block_rate"] = len(vs), round(sum(v["decision"] == "block" for v in vs) / len(vs), 4) if vs else 0.0
        if mcp_eff is not None:
            f["mcp_allowlisted"] = len(mcp_eff.get("servers", []))
        if alruns is not None:
            rs = alruns.get("items", [])
            fin = [r for r in rs if r["status"] in ("success", "failed", "blocked", "truncated", "aborted")]
            ok = [r for r in fin if r["status"] == "success"]
            f["run_count"] = len(fin)
            f["run_failure_rate"] = round(sum(r["status"] in ("failed", "blocked") for r in fin) / len(fin), 4) if fin else None
            f["run_avg_turns"] = round(sum(r["turns"] for r in ok) / len(ok), 2) if ok else None
        if cands is not None:
            cs = cands.get("items", [])
            f["bl_candidates_decided"] = sum(1 for x in cs if x.get("decision"))
            f["bl_promoted_regressions"] = sum(1 for x in cs if x["status"] == "promoted"
                                               and (x["detail"].get("metrics") or {}).get("cand_pass_hat_k", 1) < (x["detail"].get("metrics") or {}).get("base_pass_hat_k", 0) - 0.01)
        if prompts_ is not None:
            f["bl_prompts_production"] = sum(1 for p in prompts_.get("items", []) if "production" in p.get("labels", []))
        f["ev_blocked_changes_30d"] = f.get("ev_gate_fail_30d")
        return f
