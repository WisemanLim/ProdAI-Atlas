"""Manager 자가 등록 — 신규 POST · 기존 PATCH · 실패 시 재시도(fail-static)."""
import asyncio
import json

import httpx

from atlas_agent_core import registry
from units.al_plan import agent


def manager(existing=None, fail_first=0):
    calls = []

    def handler(req: httpx.Request):
        calls.append(f"{req.method} {req.url.path}")
        if len(calls) <= fail_first:
            return httpx.Response(503)
        if req.method == "GET":
            return httpx.Response(200, json={"items": existing or []})
        if req.method == "POST" and req.url.path.endswith("/deployments"):
            body = json.loads(req.content)
            return httpx.Response(201, json={"id": "dep-1", "status": "pending", **body})
        if req.method == "POST":
            return httpx.Response(201, json={"id": "new-id", "currentRev": 1, **json.loads(req.content)["metadata"]})
        return httpx.Response(200, json={"changed": False, "agent": {"id": existing[0]["id"], "currentRev": 3}})

    return httpx.MockTransport(handler), calls


def test_registers_new_agent():
    transport, calls = manager()
    state = {}
    asyncio.run(registry.register_forever("http://m", "default", agent.manifest, state, transport=transport))
    assert calls == ["GET /api/v1/agents", "POST /api/v1/agents"]
    assert state["registered"] and state["agent_id"] == "new-id"


def test_patches_existing_agent():
    transport, calls = manager(existing=[{"id": "a1", "name": "al-plan"}])
    state = {}
    asyncio.run(registry.register_forever("http://m", "default", agent.manifest, state, transport=transport))
    assert calls[-1] == "PATCH /api/v1/agents/a1" and state["rev"] == 3


def test_retries_until_manager_is_up(monkeypatch):
    async def fast_sleep(_):
        return None

    monkeypatch.setattr(registry.asyncio, "sleep", fast_sleep)
    transport, calls = manager(fail_first=2)
    state = {}
    asyncio.run(registry.register_forever("http://m", "default", agent.manifest, state, transport=transport))
    assert state["registered"] and calls.count("GET /api/v1/agents") == 3


def test_registers_deployment_with_endpoint():
    transport, calls = manager()
    state = {}
    asyncio.run(registry.register_forever("http://m", "default", agent.manifest, state, endpoint="http://agent-al-plan:8000",
                                          env="staging", transport=transport))
    assert calls[-1] == "POST /api/v1/deployments"
    assert state["deployment_id"] == "dep-1" and state["env"] == "staging"
