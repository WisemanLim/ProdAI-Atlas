"""X-SBX — 실행·상한·격리 수준·egress 정책 수명·산출물 비밀/PII 검사·실행 기록·한도 초과 이벤트."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas_agent_core import create_app, events
from units import sbx


class World:
    def __init__(self):
        self.calls, self.policies, self.last_run = [], {}, None

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        body = json.loads(req.content) if req.content else {}
        if path.startswith("/policies/"):
            pid = path.rsplit("/", 1)[1]
            if req.method == "PUT":
                assert req.headers["x-egress-admin"] == "local-egress-admin"
                self.policies[pid] = body
            else:
                self.policies.pop(pid, None)
            return httpx.Response(200, json={})
        if path == "/log":
            return httpx.Response(200, json={"items": [{"host": "example.com", "port": 443, "decision": "deny", "reason": "not_allowed", "policy": "x"}]})
        if path == "/run":
            self.last_run = body
            code = body["code"]
            if "loop" in code:
                return httpx.Response(200, json={"exit_code": -9, "stdout": "", "stderr": "", "timed_out": False, "cpu_exceeded": True, "artifacts": []})
            out = code.split("print:", 1)[1] if "print:" in code else "ok"
            return httpx.Response(200, json={"exit_code": 0, "stdout": out, "stderr": "", "timed_out": False, "duration_ms": 12,
                                             "artifacts": [{"name": "out/r.txt", "text": "key AKIA" + "A" * 16}]})
        if path == "/v1/guard/check":
            cs = body["contents"]
            if any("010-1234-5678" in c for c in cs):
                return httpx.Response(200, json={"decision": "transform", "categories": ["phone"], "contents": [c.replace("010-1234-5678", "[PHONE]") for c in cs]})
            return httpx.Response(200, json={"decision": "allow", "categories": []})
        return httpx.Response(404)


@pytest.fixture
def s(client, monkeypatch):
    from app import db
    w = World()
    monkeypatch.setattr(sbx, "TRANSPORT", httpx.MockTransport(w.handle))
    events.sent.clear()
    with TestClient(create_app(sbx.agent, engine=db.engine, public_url="http://sbx:8000")) as c:
        yield c, w


def test_run_scans_secrets_and_pii_and_records(s):
    c, w = s
    r = c.post("/v1/runs", json={"lang": "javascript", "code": "print:전화 010-1234-5678"}).json()
    assert r["status"] == "ok" and r["stdout"] == "전화 [PHONE]" and w.last_run["lang"] == "javascript"
    assert r["artifacts"][0]["text"] == "key [REDACTED:aws_access_key]"
    assert {f["type"] for f in r["findings"]} == {"aws_access_key", "pii:phone"} and "egress" not in w.last_run
    got = c.get(f"/v1/runs/{r['run_id']}").json()
    assert got["code"] == "print:전화 010-1234-5678" and got["isolation"] == "container" and len(got["code_sha256"]) == 64
    assert any(x["run_id"] == r["run_id"] for x in c.get("/v1/runs").json()["items"])


def test_egress_policy_created_logged_and_removed(s):
    c, w = s
    r = c.post("/v1/runs", json={"lang": "shell", "code": "curl", "egress": {"allow": ["fake-upstream"]}}).json()
    eg = w.last_run["egress"]
    assert eg["proxy"] == "http://egress-proxy:3128" and eg["policy"] == r["run_id"] and len(eg["token"]) > 10
    assert ("PUT", f"/policies/{r['run_id']}") in w.calls and ("DELETE", f"/policies/{r['run_id']}") in w.calls and w.policies == {}
    assert r["egress"] == {"allow": ["fake-upstream"], "decisions": [{"host": "example.com", "port": 443, "decision": "deny", "reason": "not_allowed"}]}


def test_limits_and_isolation(s):
    c, _ = s
    assert c.post("/v1/runs", json={"code": "x", "timeout_s": 9999}).status_code == 422
    bad = c.post("/v1/runs", json={"code": "x", "isolation": "microvm"})
    assert bad.status_code == 422 and "microvm" in bad.json()["detail"]
    assert c.get("/v1/isolation").json()["available"] == ["container"]
    r = c.post("/v1/runs", json={"code": "loop", "limits": {"cpu_s": 1}}).json()
    assert r["status"] == "limit_exceeded" and r["limits"]["cpu_s"] == 1
    assert any(e[0] == "plt.sbx.limit_exceeded" for e in events.sent) and any(e[0] == "plt.sbx.run.completed" for e in events.sent)


def test_scan_secrets_entropy():
    text, found = sbx.scan_secrets("tok Zx9Qa7Lm2Pw8Rt5Yu3Io1Kj6Hg4Fd0Sa9Vb7Nm2Cx")
    assert "[REDACTED:high_entropy]" in text and found[0]["type"] == "high_entropy"
    assert sbx.scan_secrets("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")[1] == []
