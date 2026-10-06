"""A2A v1.0 JSON-RPC 바인딩 — Card · 메서드 · 오류 코드 · 상태 머신 · tenant 격리."""
import asyncio

import pytest
from conftest import send

from atlas_agent_core import tasks as t

CARD_REQUIRED = {"name", "description", "supportedInterfaces", "version", "capabilities",
                 "defaultInputModes", "defaultOutputModes", "skills"}


def test_agent_card_has_a2a_v1_required_fields(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert CARD_REQUIRED <= card.keys()
    iface = card["supportedInterfaces"][0]
    assert iface["protocolBinding"] == "JSONRPC" and iface["url"].endswith("/a2a") and iface["protocolVersion"] == "1.0"
    assert card["skills"][0]["id"] == "loop.plan" and "AL-PLAN" in card["skills"][0]["tags"]


def test_send_message_completes_with_plan_artifact(rpc):
    out = send(rpc, "요구사항을 정리한다. 그리고 API 를 구현한다. 그다음 테스트한다")
    task = out["result"]["task"]
    assert task["status"]["state"] == t.COMPLETED
    plan = task["artifacts"][0]["parts"][0]["data"]
    assert [s["title"] for s in plan["steps"]] == ["요구사항을 정리한다", "API 를 구현한다", "테스트한다"]
    assert plan["steps"][2]["depends_on"] == ["s2"] and plan["dod"]


def test_get_task_and_history_length(rpc):
    tid = send(rpc, "목표 하나")["result"]["task"]["id"]
    got = rpc("GetTask", {"id": tid})["result"]
    assert got["id"] == tid and got["status"]["state"] == t.COMPLETED and len(got["history"]) == 1
    assert rpc("GetTask", {"id": tid, "historyLength": 0})["result"]["history"] == []


def test_tenant_isolation(rpc):
    tid = send(rpc, "테넌트 A 의 목표", tenant="org-a")["result"]["task"]["id"]
    assert rpc("GetTask", {"id": tid, "tenant": "org-a"})["result"]["id"] == tid
    err = rpc("GetTask", {"id": tid, "tenant": "org-b"})["error"]
    assert err["code"] == -32001
    listed = rpc("ListTasks", {"tenant": "org-b"})["result"]
    assert all(x["id"] != tid for x in listed["tasks"])


def test_list_tasks_paging_and_filter(rpc):
    ctx = "ctx-paging"
    for i in range(3):
        send(rpc, f"목표 {i}", tenant="org-page", message={"contextId": ctx})
    p1 = rpc("ListTasks", {"tenant": "org-page", "contextId": ctx, "pageSize": 2})["result"]
    assert len(p1["tasks"]) == 2 and p1["totalSize"] == 3 and p1["nextPageToken"] == "2"
    p2 = rpc("ListTasks", {"tenant": "org-page", "contextId": ctx, "pageSize": 2, "pageToken": "2"})["result"]
    assert len(p2["tasks"]) == 1 and p2["nextPageToken"] == ""
    assert rpc("ListTasks", {"tenant": "org-page", "status": t.FAILED})["result"]["totalSize"] == 0


def test_input_required_then_continue(rpc):
    first = rpc("SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"data": {"goal": ""}}]}})["result"]["task"]
    assert first["status"]["state"] == t.INPUT_REQUIRED
    assert "goal" in first["status"]["message"]["parts"][0]["text"]
    cont = send(rpc, "이제 목표를 보낸다", message={"taskId": first["id"]})["result"]["task"]
    assert cont["id"] == first["id"] and cont["status"]["state"] == t.COMPLETED
    assert len(cont["history"]) == 3  # user → agent(질문) → user


def test_terminal_task_rejects_messages_and_cancel(rpc):
    tid = send(rpc, "끝난 작업")["result"]["task"]["id"]
    assert send(rpc, "더 보냄", message={"taskId": tid})["error"]["code"] == -32004
    assert rpc("CancelTask", {"id": tid})["error"]["code"] == -32002


def test_cancel_non_terminal_task(rpc):
    tid = rpc("SendMessage", {"message": {"parts": [{"text": ""}]}})["result"]["task"]["id"]  # INPUT_REQUIRED
    assert rpc("CancelTask", {"id": tid})["result"]["status"]["state"] == t.CANCELED


@pytest.mark.parametrize("body,code", [
    ({"jsonrpc": "1.0", "id": 1, "method": "GetTask"}, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": "Nope"}, -32601),
    ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"parts": []}}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"parts": [{"text": "x"}]}, "metadata": {"skill": "x.y"}}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "missing"}}, -32001),
    ({"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {"pageSize": 0}}, -32602),
])
def test_jsonrpc_errors(client, body, code):
    assert client.post("/a2a", json=body).json()["error"]["code"] == code


def test_parse_error(client):
    res = client.post("/a2a", content=b"{not json", headers={"content-type": "application/json"})
    assert res.json()["error"]["code"] == -32700


def test_return_immediately_runs_in_background(rpc):
    out = send(rpc, "백그라운드 목표", configuration={"returnImmediately": True})
    task = out["result"]["task"]
    assert task["status"]["state"] == t.WORKING
    for _ in range(50):
        state = rpc("GetTask", {"id": task["id"]})["result"]["status"]["state"]
        if state == t.COMPLETED:
            break
        asyncio.run(asyncio.sleep(0.02))
    assert state == t.COMPLETED


def test_state_machine_rejects_invalid_transitions():
    assert t.COMPLETED not in t.TRANSITIONS[t.SUBMITTED]
    for terminal in t.TERMINAL:
        assert terminal not in t.TRANSITIONS  # 종료 상태에서 나가는 전이 없음
    assert len(t.STATES) == 8
