def test_health_ok(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["db"]["status"] == "ok"


def test_liveness_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    body = client.get("/readyz").json()
    assert body["status"] == "ok" and body["unit"] == "AL-PLAN"
    assert body["registration"]["enabled"] is False


def test_trace_exclusion_rule():
    from atlas_agent_core.server import skip_trace
    assert skip_trace({"type": "http", "method": "GET", "path": "/readyz", "headers": []})
    assert skip_trace({"type": "http", "method": "GET", "path": "/runs", "headers": []})
    assert not skip_trace({"type": "http", "method": "GET", "path": "/runs", "headers": [(b"traceparent", b"00-ab-cd-01")]})
    assert not skip_trace({"type": "http", "method": "POST", "path": "/a2a", "headers": []})
