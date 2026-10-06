"""AL-RUN A2A 표면 — loop.run · HITL(INPUT_REQUIRED → approve) · /runs 조회 · 이벤트 SSE."""
import pytest
from fastapi.testclient import TestClient
from loop_fakes import ScriptGateway

from atlas_agent_core import create_app
from units import al_run
from units.al_run.clients import PlannerClient


@pytest.fixture(scope="module")
def run_client(client):
    from app import db
    app = create_app(al_run.agent, engine=db.engine, public_url="http://al-run:8000", env="test")
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fake_clients(monkeypatch):
    monkeypatch.setattr(al_run.clients, "from_env", lambda: (ScriptGateway(), PlannerClient(None)))


def send(c, data=None, text=None, task_id=None, tenant="org-r"):
    parts = [{"data": data}] if data is not None else [{"text": text}]
    msg = {"role": "ROLE_USER", "parts": parts, **({"taskId": task_id} if task_id else {})}
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"tenant": tenant, "message": msg}}).json()


def test_card_lists_run_skills(run_client):
    card = run_client.get("/.well-known/agent-card.json").json()
    assert [s["id"] for s in card["skills"]] == ["loop.run", "loop.resume"]


def test_loop_run_goal_verified(run_client):
    task = send(run_client, {"goal": 'atlas_calc({"expr":"2+3"}) 로 계산', "dod": [{"type": "test", "spec": "contains:5"}]})["result"]["task"]
    res = task["artifacts"][0]["parts"][0]["data"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and res["reason"] == "GOAL_VERIFIED" and res["output"] == "[mock:final] 5"
    run = run_client.get(f"/runs/{task['id']}", params={"tenant": "org-r"}).json()
    assert run["status"] == "success" and run["result"]["turns"] == 1


def test_hitl_over_a2a_and_event_stream(run_client):
    first = send(run_client, {"goal": "atlas_delete_all 로 정리"})["result"]["task"]
    assert first["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "atlas_delete_all" in first["status"]["message"]["parts"][0]["text"]
    done = send(run_client, {"approval": "approve"}, task_id=first["id"])["result"]["task"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert done["artifacts"][0]["parts"][0]["data"]["reason"] == "END_TURN"
    sse = run_client.get(f"/runs/{first['id']}/events", params={"tenant": "org-r"}).text
    kinds = [line.split(": ", 1)[1] for line in sse.splitlines() if line.startswith("event: ")]
    assert kinds[0] == "plan" and "hitl" in kinds and kinds[-1] == "exit" and kinds.count("exit") == 2  # pending + 최종


def test_tenant_isolation_of_runs(run_client):
    task = send(run_client, {"goal": "인사"}, tenant="org-x")["result"]["task"]
    assert run_client.get(f"/runs/{task['id']}", params={"tenant": "org-y"}).status_code == 404


def test_loop_resume_skill_requires_checkpoint(run_client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"metadata": {"skill": "loop.resume"}, "message": {"parts": [{"data": {"run_id": "missing"}}]}}}
    task = run_client.post("/a2a", json=body).json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED" and "체크포인트 없음" in task["status"]["message"]["parts"][0]["text"]


def test_list_runs_by_tenant(run_client):
    send(run_client, {"goal": "목록용 목표"}, tenant="org-list")
    items = run_client.get("/runs", params={"tenant": "org-list"}).json()["items"]
    assert items and items[0]["status"] == "success" and run_client.get("/runs", params={"tenant": "org-none"}).json()["items"] == []


def test_hitl_posts_to_approval_inbox_and_resumes_with_edits(run_client, monkeypatch):
    import httpx

    from units.al_run.engine import _apply_edits
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), req.content))
        return httpx.Response(201, json={"id": "ap-1"})
    monkeypatch.setattr(al_run, "_http", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.delenv("ATLAS_MANAGER_TOKEN", raising=False)   # 컨테이너 env — 토큰 발급 없이(개발 모드) 호출
    monkeypatch.setitem(al_run.agent.resources, "manager_url", "http://manager:8080")
    monkeypatch.setitem(al_run.agent.resources, "registration", {"agent_id": "ag-run", "registered": True})
    first = send(run_client, {"goal": "atlas_delete_all 로 정리"}, tenant="org-h")["result"]["task"]
    assert first["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    import json
    url, body = seen[0]
    req = json.loads(body)
    assert url.startswith("http://manager:8080/api/v1/approvals?org=org-h") and req["sourceRef"] == first["id"] and req["agentId"] == "ag-run"
    assert req["source"] == "loop" and "atlas_delete_all" in req["action"] and req["payload"]["calls"]
    done = send(run_client, {"approval": "approve", "edited_arguments": {"scope": "tmp-only"}}, task_id=first["id"], tenant="org-h")["result"]["task"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    wd = [json.loads(b) for u, b in seen if "/approvals/withdraw" in u]
    assert wd == [{"sourceRef": first["id"], "agentId": "ag-run"}]
    sse = run_client.get(f"/runs/{first['id']}/events", params={"tenant": "org-h"}).text
    assert "event: hitl_edit" in sse
    # 다중 호출: {call_id: args} · 단일: args 그대로
    p = {"calls": [{"id": "a", "function": {"arguments": "{}"}}, {"id": "b", "function": {"arguments": "{}"}}]}
    p["all_calls"] = p["calls"]
    _apply_edits(p, {"b": {"x": 1}})
    assert p["calls"][0]["function"]["arguments"] == "{}" and json.loads(p["calls"][1]["function"]["arguments"]) == {"x": 1}
