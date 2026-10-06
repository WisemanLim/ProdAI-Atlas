"""X-RPT — X-NTF webhook 서명 수집·멱등 · 비용 그룹핑(통화·CSV·PDF) · 통합 감사(해시 체인·추가 전용·WORM·검색) · 차지백 · SIEM(OCSF·HEC·버퍼)
· 이상 탐지(EWMA z) · 월간 보고서(4차트·메일 규칙·이벤트)."""
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas_agent_core import create_app, events
from units import rpt

ADMIN = {"authorization": "Bearer local-rpt-admin"}
SECRET = "local-rpt-ingest"


class World:
    def __init__(self):
        self.hec, self.down, self.rules = [], False, []

    def handle(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/services/collector/event":
            if req.headers.get("authorization") != "Splunk hec-tok":
                return httpx.Response(401)
            if self.down:
                return httpx.Response(503)
            self.hec += [json.loads(x) for x in req.content.decode().splitlines()]
            return httpx.Response(200, json={"code": 0})
        if p == "/v1/band":
            return httpx.Response(200, json={"stage": 2, "score_by_attr": {"Reliable": 80, "Safe": 60}})
        if p == "/v1/metrics":
            return httpx.Response(200, json={"items": [{"name": "task_success_rate", "value": 0.9}, {"name": "guardrail_block_rate", "value": 0.05}]})
        if p.endswith("/notifications/rules"):
            if req.method == "POST":
                self.rules.append(json.loads(req.content))
                return httpx.Response(201, json={"id": f"r{len(self.rules)}"})
            return httpx.Response(200, json={"items": [{"id": "r", "name": r["name"]} for r in self.rules]})
        return httpx.Response(404)


@pytest.fixture
def r(client, monkeypatch, tmp_path):
    from app import db
    w = World()
    monkeypatch.setattr(rpt, "TRANSPORT", httpx.MockTransport(w.handle))
    monkeypatch.setenv("ATLAS_RPT_WORM_DIR", str(tmp_path / "worm"))
    monkeypatch.setenv("ATLAS_MANAGER_URL", "http://manager:8080")
    monkeypatch.setenv("ATLAS_RPT_INTERVAL_S", "3600")
    monkeypatch.setattr(rpt.secrets, "get", fake_secret)
    events.sent.clear()
    with TestClient(create_app(rpt.agent, engine=db.engine, public_url="http://rpt:8000")) as c:
        yield c, w


async def fake_secret(name, env=None, dev_default=None, fresh=False):
    return {"siem-hec-token": "hec-tok"}.get(name, dev_default or "")


def post_event(c, topic, data, org="default", eid=None, secret=SECRET, ts=None):
    body = json.dumps({"id": eid or f"e-{time.time_ns()}", "topic": topic, "org": org, "time": (ts or datetime.now(UTC)).isoformat(), "data": data}).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return c.post("/v1/ingest/events", content=body, headers={"X-Atlas-Signature": f"t={t},v1={sig}", "X-Atlas-Event": topic})


def gw(rid, project="p1", model="gpt-x", cost="0.002", agent=None, tenant="t-rpt"):
    return {"request_id": rid, "tenant": tenant, "project": project, "model": model, "cost_usd": cost, "input": 100, "output": 50, "cached_read": 10,
            "tags": {"agent": agent} if agent else {}}


def test_ingest_signature_idempotent_and_usage_report(r):
    c, _ = r
    assert post_event(c, "gw.request.completed", gw("q0"), secret="wrong").status_code == 401
    assert post_event(c, "gw.request.completed", gw("q1")).json() == {"usage": 4, "audit": 0}
    assert post_event(c, "gw.request.completed", gw("q1")).json()["usage"] == 0               # 같은 request_id 재전송
    post_event(c, "gw.request.completed", gw("q2", project="p2", model="claude-y", cost="0.010"))
    post_event(c, "plt.sbx.run.completed", {"run": "run-1", "duration_ms": 2000, "lang": "python", "status": "ok"}, org="t-rpt")
    assert c.post("/v1/usage", json={"events": [{"tenant": "t-rpt", "project": "p1", "meter": "tool_calls", "qty": 3, "agent_id": "a1"}]}).json()["accepted"] == 1
    rep = c.get("/v1/reports/usage", params={"tenant": "t-rpt", "group_by": "project,model", "currency": "KRW"}).json()
    rows = {(i["project"], i["model"]): i for i in rep["items"]}
    assert rows[("p2", "claude-y")]["cost"] == 13.8 and rows[("p1", "gpt-x")]["tokens_in"] == 100 and rows[("default", "")]["sbx_cpu_s"] == 2.0
    assert rows[("p1", "")]["tool_calls"] == 3 and rep["currency"] == "KRW"
    csv_ = c.get("/v1/reports/usage", params={"tenant": "t-rpt", "group_by": "project", "format": "csv"})
    assert csv_.headers["content-type"].startswith("text/csv") and csv_.text.splitlines()[0].startswith("project,cost")
    pdf = c.get("/v1/reports/usage", params={"tenant": "t-rpt", "group_by": "model", "format": "pdf"})
    assert pdf.content.startswith(b"%PDF-1.4") and pdf.content.rstrip().endswith(b"%%EOF")
    assert c.get("/v1/reports/usage", params={"group_by": "nope"}).status_code == 422
    assert c.get("/v1/reports/usage", params={"format": "parquet"}).status_code == 415


def test_audit_chain_append_only_worm_and_search(r):
    c, _ = r
    from app import db
    mgr = {"org": "t-aud", "id": "m-1", "seq": 1, "ts": "2026-10-02T01:00:00Z", "actorType": "user", "actorId": "kim", "actorIp": "10.0.0.1",
           "action": "agent.create", "resource": "agent:a1", "result": "success", "beforeHash": "", "afterHash": "ab", "detail": '{"name": "faq"}', "hash": "h"}
    assert post_event(c, "audit.recorded", mgr, org="t-aud").json()["audit"] == 1
    assert post_event(c, "audit.recorded", mgr, org="t-aud").json()["audit"] == 0                  # 같은 Manager 감사 id
    post_event(c, "plt.mkt.installed", {"install": "i-1", "template": "faq-agent"}, org="t-aud")
    post_event(c, "plt.kb.erasure.completed", {"subject_sha256": "abc", "certificate": "c1"}, org="t-aud")
    post_event(c, "plt.unrelated.thing", {"x": 1}, org="t-aud")
    items = c.get("/v1/audit", params={"tenant": "t-aud"}).json()["items"]
    assert [i["action"] for i in items] == ["plt.kb.erasure.completed", "plt.mkt.installed", "agent.create"]
    assert items[-1]["actor"] == "kim" and items[-1]["ip"] == "10.0.0.1" and items[-1]["after"]["detail"] == {"name": "faq"}
    assert [i["action"] for i in c.get("/v1/audit", params={"tenant": "t-aud", "action": "plt.mkt."}).json()["items"]] == ["plt.mkt.installed"]
    assert c.get("/v1/audit", params={"tenant": "t-aud", "actor": "kim"}).json()["items"][0]["resource"] == "agent:a1"
    v = c.get("/v1/audit/verify").json()
    assert v["valid"] and v["worm"]["match"] and v["worm"]["segments"] >= 1 and v["retention_days"] == 365
    with pytest.raises(Exception, match="append-only"), db.engine.begin() as x:
        x.execute(text("UPDATE rpt_audit SET actor = 'mallory' WHERE tenant = 't-aud'"))
    with pytest.raises(Exception, match="append-only"), db.engine.begin() as x:
        x.execute(text("DELETE FROM rpt_audit WHERE tenant = 't-aud'"))


def test_chargeback_invoice(r):
    c, _ = r
    for i in range(3):
        post_event(c, "gw.request.completed", gw(f"cb-{i}", project="dept-a", cost="1.0", tenant="t-cb"))
    post_event(c, "gw.request.completed", gw("cb-x", project="dept-b", cost="2.0", tenant="t-cb"))
    assert c.put("/v1/rates/gold", json={"meters": {"cost": {"unit_price": 1.0, "markup_pct": 10}, "tokens_out": {"unit_price": 0.001}}}).status_code == 401
    c.put("/v1/rates/gold", json={"currency": "USD", "meters": {"cost": {"unit_price": 1.0, "markup_pct": 10}, "tokens_out": {"unit_price": 0.001}}}, headers=ADMIN)
    c.put("/v1/plans/t-cb/dept-a", json={"plan": "gold", "department": "영업"}, headers=ADMIN)
    month = datetime.now(UTC).strftime("%Y-%m")
    out = c.post("/v1/chargeback/close", json={"month": month, "tenant": "t-cb"}, headers=ADMIN).json()
    inv = {i["project"]: i for i in c.get("/v1/invoices", params={"month": month, "tenant": "t-cb"}).json()["items"]}
    a = inv["dept-a"]
    assert len(out["invoices"]) == 2 and a["plan"] == "gold" and a["department"] == "영업"
    lines = {x["meter"]: x for x in a["lines"]}
    assert lines["requests"]["amount"] == 3.0 and lines["tokens_out"]["amount"] == 0.15 and a["total"] == round(3.0 * 1.1 + 0.15, 6)
    assert inv["dept-b"]["plan"] == "standard" and inv["dept-b"]["total"] == 2.0
    assert "plt.rpt.chargeback.closed" in [e[0] for e in events.sent]


def test_siem_hec_ocsf_buffering(r):
    c, w = r
    post_event(c, "plt.mkt.installed", {"install": "s-1"}, org="t-siem")
    c.put("/v1/siem/sinks/splunk", json={"type": "splunk_hec", "url": "http://siem/services/collector/event", "secret": "siem-hec-token",
                                          "filter": ["plt.mkt."]}, headers=ADMIN)
    assert c.post("/v1/siem/flush").json()["items"][0]["error"] is None
    ev = [e for e in w.hec if e["event"]["unmapped"]["source"] == "event"][-1]
    assert ev["sourcetype"] == "ocsf:api_activity" and ev["event"]["class_uid"] == 6003 and ev["event"]["api"]["operation"].startswith("plt.mkt.")
    w.down = True
    post_event(c, "plt.mkt.installed", {"install": "s-2"}, org="t-siem")
    assert c.post("/v1/siem/flush").json()["items"][0]["error"].startswith("503")
    s = c.get("/v1/siem/sinks").json()["items"][0]
    assert s["buffered"] >= 1 and s["failures"] == 1
    w.down = False
    c.post("/v1/siem/flush")
    s = c.get("/v1/siem/sinks").json()["items"][0]
    assert s["buffered"] == 0 and s["last_error"] is None and any(e["event"]["resources"][0]["name"] == "install:s-2" for e in w.hec)


def test_anomaly_ewma_z(r):
    c, _ = r
    now = datetime.now(UTC)
    ev = [{"id": f"base-{i}", "ts": (now - timedelta(seconds=300 * (10 - i))).isoformat(), "tenant": "t-an", "agent_id": "bot-1", "meter": "tokens_in",
           "qty": 100 + (i % 3) * 5} for i in range(10)]
    ev.append({"id": "spike", "ts": now.isoformat(), "tenant": "t-an", "agent_id": "bot-1", "meter": "tokens_in", "qty": 5000})
    c.post("/v1/usage", json={"events": ev})
    d = c.post("/v1/anomaly/scan", json={"tenant": "t-an", "window_s": 300, "lookback": 24}).json()["detected"]
    assert len(d) == 1 and d[0]["agent_id"] == "bot-1" and d[0]["z"] > 3 and d[0]["traces"] == ["spike"]
    assert c.post("/v1/anomaly/scan", json={"tenant": "t-an", "window_s": 300, "lookback": 24}).json()["detected"] == []      # 같은 창은 1회
    e = [x for x in events.sent if x[0] == "plt.rpt.usage.anomaly"][-1][1]
    assert e["trace_links"] == ["/svc/ob/v1/traces?request_id=spike"]
    assert c.get("/v1/anomalies", params={"tenant": "t-an"}).json()["items"][0]["meter"] == "tokens"


def test_monthly_report_charts_mail(r):
    c, w = r
    post_event(c, "gw.request.completed", gw("mo-1", project="default", cost="0.5", tenant="t-mo"))
    month = datetime.now(UTC).strftime("%Y-%m")
    rep = c.post("/v1/reports/monthly", json={"month": month, "tenant": "t-mo", "send_to": ["ceo@example.test"]}, headers=ADMIN).json()
    ch = rep["charts"]
    assert (ch["task_success_rate"], ch["guardrail_block_rate"], ch["mat_score"], ch["mat_stage"]) == (0.9, 0.05, 70.0, 2)
    assert ch["cost_per_task"][0]["value"] == 0.5 and rep["summary"]["cost_usd"] == 0.5 and rep["mail"] == "queued"
    html = c.get(rep["html"]).text
    assert html.count("<svg") == 4 and "월간" in html
    assert c.get(rep["pdf"]).content.startswith(b"%PDF")
    rule = next(x for x in w.rules if x["name"].startswith("x-rpt-monthly-"))
    assert rule["channels"] == [{"type": "email", "to": ["ceo@example.test"]}] and rule["topic"] == "plt.rpt.report.ready"
    ready = [x for x in events.sent if x[0] == "plt.rpt.report.ready"][-1][1]
    assert ready["recipients"] == rule["filter"][0]["value"] and ready["report"] == rep["id"]


def test_worm_concurrent_seal_no_overlap(r):
    """동시 봉인(워커 2·주기 작업) — 잠금으로 겹치는 세그먼트 없이 행마다 정확히 1회."""
    import threading

    from units import rpt
    c, _ = r
    for i in range(5):
        post_event(c, "plt.mkt.installed", {"install": f"w-{i}", "template": "t"}, org="t-worm")
    ts = [threading.Thread(target=rpt.seal_worm) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    v = c.get("/v1/audit/verify").json()
    assert v["worm"]["match"] and v["worm"]["duplicates"] == 0 and v["worm"]["events"] == v["events"]
