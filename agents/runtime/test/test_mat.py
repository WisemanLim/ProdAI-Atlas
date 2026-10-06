"""MAT — 수집(가짜 플랫폼) → 4속성·단계(연속 게이트)·커버리지·복리 루프·로드맵·지표·띠·스냅샷(단계 변경 이벤트)·리포트·성과."""
import httpx
import pytest
from fastapi.testclient import TestClient

from atlas_agent_core import create_app, events
from units import mat
from units.mat import collect, engine


def platform(level: int):
    """level 이 높을수록 성숙한 플랫폼의 응답."""
    ready = [{"agentId": f"a{i}", "status": "ready"} for i in range(4)]
    agents = [{"id": "a0", "concept": "01-agentic-loops"}, {"id": "a1", "concept": "06-evals"}, {"id": "a2", "concept": "07-guardrails"},
              {"id": "a3", "concept": "03-multi-agent" if level >= 4 else "02-mcp"}]
    approved = 60 if level >= 2 else 10
    R = {
        "/api/v1/agents": {"items": agents}, "/api/v1/deployments": {"items": ready},
        "/api/v1/approvals": {"items": [{"createdAt": "2026-10-02T00:00:00Z", "decidedAt": "2026-10-02T00:02:00Z"}] if level >= 4 else []},
        "/api/v1/orgs/default/audit/verify": {"valid": True},
        "/api/v1/a2a/registry/agents": {"items": [{"trust": "signed"}] * (5 if level >= 4 else 1)},
        "/admin/budgets": {"data": [{"scope": "org"}, *([{"scope": "key"}] if level >= 2 else [])]},
        "/admin/econ/cache": {"data": [{"hit_ratio": 0.35 if level >= 3 else 0.05, "requests": 10}]},
        "/admin/compute-policies": {"data": [{"id": "p"}] if level >= 3 else []},
        "/v1/dashboards/llm": {"summary": [{"requests": 100, "input_tokens": 5000, "cost_usd": 0.4, "error_rate": 0.01}]},
        "/v1/slos": {"items": [{"id": "s"}] if level >= 3 else []}, "/v1/alerts/rules": {"items": [{"id": "r"}]},
        "/v1/datasets": {"items": [{"name": "gold", "counts": {"approved": approved, "draft": 0, "in_review": 1}}]},
        "/v1/datasets/gold/items": {"items": [{"source": "prod-trace", "status": "approved"}, {"source": "manual", "status": "approved"}] if level >= 3 else []},
        "/v1/runs": {"items": [{"pass_rate": 0.95, "gate": "pass", "created_at": "2026-10-01T00:00:00+00:00"},
                               {"pass_rate": 0.92, "gate": "fail", "created_at": "2026-10-01T01:00:00+00:00"}]},
        "/v1/judges": {"items": [{"calibration": {"trusted": True}}] if level >= 3 else []},
        "/v1/online-rules": {"items": [{"enabled": True}] if level >= 3 else []},
        "/v1/scorecard": {"items": [{"task_success_rate": 0.9, "cost_per_task_usd": 0.002}]},
        "/v1/guard/verdicts": {"items": [{"decision": "allow"}, {"decision": "block"}]},
        "/v0.1/tenants/default/effective": {"servers": [{"name": "x"}]},
        "/runs": {"items": [{"status": "success", "turns": 3}, {"status": "failed", "turns": 9}]},
        "/v1/candidates": {"items": [{"status": "promoted", "decision": "promote", "detail": {"metrics": {"base_pass_hat_k": 0.9, "cand_pass_hat_k": 0.9}}}] if level >= 3 else []},
        "/v1/prompts": {"items": [{"labels": ["production"]}] if level >= 4 else []},
    }

    def handle(req: httpx.Request) -> httpx.Response:
        body = R.get(req.url.path)
        if req.url.host == "agent-ob" and level < 0:
            return httpx.Response(503)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, json={})
    return httpx.MockTransport(handle)


@pytest.fixture
def mc(client, monkeypatch):
    from app import db
    monkeypatch.setenv("ATLAS_GATEWAY_ADMIN_TOKEN", "t")
    events.sent.clear()
    with TestClient(create_app(mat.agent, engine=db.engine, public_url="http://mat:8000")) as c:
        yield c, monkeypatch


def use(mp, level):
    mp.setattr(collect, "TRANSPORT", platform(level))
    mat._cache.clear()


def test_levels_gates_coverage_roadmap(mc):
    c, mp = mc
    use(mp, 1)
    a = c.get("/v1/assessment", params={"project": "p-low"}).json()
    assert a["stage"]["level"] == 1 and a["band"]["phase"] == "Production" and "golden_set_50" in a["band"]["unmet_gates"]
    assert a["attributes"]["reliable"]["score"] < a["attributes"]["safe"]["score"] and a["source_errors"] == {}
    cards = a["roadmap"]["cards"]
    assert cards[0]["gate"].startswith("L2:") and all(1 <= cd["weeks"][0] <= cd["weeks"][1] <= 13 for cd in cards)
    assert any(i["title"].startswith("[MAT L2]") for i in c.get("/v1/roadmap", params={"project": "p-low", "format": "github"}).json()["issues"])
    assert "| 주차 | 단계 |" in c.get("/v1/roadmap", params={"project": "p-low", "format": "markdown"}).text
    use(mp, 4)
    hi = c.get("/v1/assessment", params={"project": "p-hi"}).json()
    assert hi["stage"]["level"] == 4 and hi["band"]["progress_pct"] == 100 and hi["stage"]["next"] is None
    cov = hi["coverage"]
    assert cov["matrix"]["01-agentic-loops"]["reliable"] == "●" and cov["matrix"]["09-bitter-lesson"]["safe"] == "" and cov["active"]["a2a"] == 5
    m = {x["name"]: x["value"] for x in hi["metrics"]}
    assert m == {"task_success_rate": 0.9, "cost_per_task": 0.002, "cache_hit_rate": 0.35, "tool_error_rate": 0.5, "guardrail_block_rate": 0.5,
                 "hitl_approval_latency": 120.0, "turns_to_completion": 3.0}
    assert hi["compound"]["loop_running"] is True and hi["compound"]["promotions_in_regression_rate"] == 1.0


def test_consecutive_gates_and_unknown_sources():
    d = mat.DEFINITION
    facts = {"ob_requests_24h": 1, "gr_verdicts": 1, "mgr_audit_valid": True, "gw_budgets": 1}   # L1 만, L3 사실 일부
    facts.update({"ev_judges_trusted": 1, "ev_online_rules": 1})
    st = engine.stage(d, facts)
    assert st["level"] == 1 and [g["result"] for g in st["levels"][1]["gates"]][:1] == ["unknown"]   # 사실 없음 = unknown (pass 아님)
    assert engine.judge("x >= 1", {}) == "unknown" and engine.judge("x >= 1", {"x": None}) == "unknown"


def test_snapshots_events_report_outcomes(mc):
    c, mp = mc
    use(mp, 1)
    s1 = c.post("/v1/snapshots", params={"project": "p-snap"}).json()
    use(mp, 4)
    s2 = c.post("/v1/snapshots", params={"project": "p-snap"}).json()
    assert (s1["level"], s2["level"]) == (1, 4) and ("plt.mat.stage_changed", {"project": "p-snap", "from": 1, "to": 4, "name": "Autonomous Multi-agent"}) in events.sent
    assert len(c.get("/v1/snapshots", params={"project": "p-snap"}).json()["items"]) == 2
    out = c.get("/v1/assessment", params={"project": "p-snap", "fresh": True}).json()["outcomes"]
    assert out["baseline_at"] == s1["created_at"] and all(x["change_pct"] == 0.0 for x in out["cards"])
    rep = c.get("/v1/report", params={"project": "p-snap"}).text
    assert "L4 Autonomous Multi-agent" in rep and "## 조직 롤업" in rep and "p-snap" in rep
    b = c.get("/v1/band", params={"project": "p-snap"}).json()
    assert set(b["score_by_attr"]) == {"Reliable", "Observable", "Cost-aware", "Safe"}


def test_source_down_is_reported(mc):
    c, mp = mc
    use(mp, -1)
    a = c.get("/v1/assessment", params={"project": "p-down"}).json()
    assert "ob.dashboard" in a["source_errors"] and a["stage"]["level"] == 0
