"""Observability — 검색 DSL(주입 방지) · span 트리 · 승격 I/O · 대시보드·SLO·경보 (ClickHouse 는 가짜 HTTP)."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas_agent_core import create_app
from units import ob
from units.ob import queries
from units.ob.ch import ClickHouse
from units.ob.dsl import DslError, compile_query


def test_dsl_compiles_with_bound_params():
    c = compile_query('gen_ai.request.model = "planner-default" AND atlas.cost.usd > 0.0001 AND status = error AND name ~ "chat" AND duration_ms >= 5')
    assert "SpanAttributes[{q0k:String}] = {q0:String}" in c.where and c.params["q0k"] == "gen_ai.request.model"
    assert "toFloat64OrNull(SpanAttributes[{q1k:String}]) > {q1:Float64}" in c.where
    assert c.params["q2"] == "Error" and "positionCaseInsensitive(SpanName" in c.where and "Duration / 1e6 >= {q4:Float64}" in c.where
    assert compile_query("").where == "1"


@pytest.mark.parametrize("bad", ['name = "x" OR 1=1', "x' ; DROP TABLE otel_traces --", 'a ~ 5 AND', 'status > "x"', 'duration_ms ~ 1', 'name = "unclosed'])
def test_dsl_rejects_bad_or_injection(bad):
    with pytest.raises(DslError):
        compile_query(bad)


def test_dsl_quoted_and_inside_value():
    c = compile_query('name = "A AND B"')
    assert c.params["q0"] == "A AND B"


ROWS = [
    {"span_id": "a", "parent_span_id": "x", "name": "invoke_agent al-run", "service": "atlas-agent-al-run", "kind": "Server", "start_ns": 1_000_000_000,
     "duration_ns": 50_000_000, "status": "Unset", "status_message": "", "attributes": {}, "event_names": ["atlas.loop.exit"], "event_attributes": [{"reason": "END_TURN"}]},
    {"span_id": "b", "parent_span_id": "a", "name": "chat agent-default", "service": "atlas-gateway", "kind": "Server", "start_ns": 1_010_000_000,
     "duration_ns": 5_000_000, "status": "Unset", "status_message": "", "event_names": [], "event_attributes": [],
     "attributes": {"gen_ai.request.model": "agent-default", "gen_ai.input.messages": json.dumps([{"role": "system", "content": "s"}, {"role": "user", "content": "목표"}]),
                    "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": "", "tool_calls": [{}]}])}},
    {"span_id": "c", "parent_span_id": "a", "name": "chat agent-default", "service": "atlas-gateway", "kind": "Server", "start_ns": 1_030_000_000,
     "duration_ns": 5_000_000, "status": "Unset", "status_message": "", "event_names": [], "event_attributes": [],
     "attributes": {"gen_ai.input.messages": json.dumps([{"role": "user", "content": "목표"}, {"role": "tool", "content": "42"}]),
                    "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": "답은 42"}])}},
]


def test_tree_waterfall_graph():
    t = queries.tree([dict(r) for r in ROWS])
    root = t["roots"][0]
    assert t["spans"] == 3 and root["span_id"] == "a" and len(root["children"]) == 2
    assert [w["offset_ms"] for w in t["waterfall"]] == [0.0, 10.0, 30.0] and root["events"][0]["name"] == "atlas.loop.exit"
    assert t["graph"] == [{"from": "atlas-agent-al-run", "to": "atlas-gateway", "calls": 2}]


def test_trace_io_for_promotion():
    io = queries.trace_io([dict(r) for r in ROWS])
    assert io == {"input": "목표", "output": "답은 42", "tool_results": ["42"], "model": "agent-default", "captured": True}


class FakeCH:
    """SQL 의 특징 문자열로 응답을 고른다."""

    def __init__(self, total=100, bad=10, summary=None):
        self.total, self.bad, self.sql = total, bad, []
        self.summary = summary or {"grp": "all", "requests": 100, "errors": 10, "error_rate": 0.1, "p50_ms": 1, "p95_ms": 5, "cost_usd": 0.01}

    def transport(self):
        def handler(req: httpx.Request):
            sql = req.content.decode()
            self.sql.append((sql, dict(req.url.params)))
            if "countIf(" in sql and "AS bad" in sql:
                data = [{"total": self.total, "bad": self.bad}]
            elif "DISTINCT TraceId AS t" in sql:
                data = [{"t": "trace-1"}, {"t": "trace-2"}]
            elif "toStartOfMinute" in sql:
                data = []
            elif "AS grp" in sql:
                data = [self.summary]
            else:
                data = []
            return httpx.Response(200, json={"data": data})
        return httpx.MockTransport(handler)


@pytest.fixture
def ob_client(client, monkeypatch):
    from app import db
    fake = FakeCH()
    monkeypatch.setattr(ob, "ch", lambda: ClickHouse(url="http://ch", transport=fake.transport()))
    monkeypatch.setenv("ATLAS_OB_ECHO_RECEIVER", "true")
    with TestClient(create_app(ob.agent, engine=db.engine, public_url="http://ob:8000")) as c:
        c.fake = fake
        yield c


def test_slo_status_burn_rate(ob_client):
    slo = ob_client.post("/v1/slos", json={"name": "chat 오류율", "kind": "error_rate", "objective": 0.99}).json()
    st = ob_client.get(f"/v1/slos/{slo['id']}/status").json()
    assert st["windows"]["1h"] == {"total": 100, "bad": 10, "sli": 0.9, "burn_rate": 10.0}
    assert st["error_budget_remaining"] == -9.0
    assert ob_client.post("/v1/slos", json={"name": "x", "kind": "latency", "objective": 0.99}).status_code == 422
    assert ob_client.post("/v1/slos", json={"name": "x", "kind": "error_rate", "objective": 0.99, "filter": "bad filter ("}).status_code == 422


def test_alert_fires_once_with_trace_links_and_resolves(ob_client, monkeypatch):
    rule = ob_client.post("/v1/alerts/rules", json={"name": "오류율 5% 초과", "severity": "critical", "webhook_url": "http://testserver/v1/alerts/echo",
                                                    "expr": {"type": "metric", "metric": "error_rate", "op": ">", "value": 0.05}}).json()
    sent = []

    async def record(url, payload):
        sent.append(payload)
        return "sent"
    monkeypatch.setattr(ob, "send_webhook", record)
    first = asyncio.run(ob.evaluate_rules())
    again = asyncio.run(ob.evaluate_rules())                                  # 계속 firing → 중복 알림 없음
    assert len(first) == 1 and first[0]["status"] == "firing" and first[0]["delivered"] == "sent" and again == []
    assert sent[0]["traces"] == ["http://agent-ob:8000/v1/traces/trace-1", "http://agent-ob:8000/v1/traces/trace-2"]
    ob_client.fake.summary = {**ob_client.fake.summary, "error_rate": 0.0}
    resolved = asyncio.run(ob.evaluate_rules())
    assert resolved[0]["status"] == "resolved"
    events = ob_client.get("/v1/alerts/events", params={"rule_id": rule["id"]}).json()["items"]
    assert [e["status"] for e in events] == ["resolved", "firing"]


def test_dashboard_and_costs_validate_group(ob_client):
    assert ob_client.get("/v1/dashboards/llm", params={"group_by": "model"}).json()["summary"][0]["requests"] == 100
    assert ob_client.get("/v1/dashboards/llm", params={"group_by": "evil"}).status_code == 422
    assert ob_client.get("/v1/costs", params={"group_by": "nope"}).status_code == 422
    sql, params = ob_client.fake.sql[0]
    assert "param_since" in params and "SpanAttributes['gen_ai.request.model'] AS grp" in sql


def test_trace_search_rejects_bad_dsl(ob_client):
    assert ob_client.get("/v1/traces", params={"q": "x = 1 OR 1=1"}).status_code == 422
