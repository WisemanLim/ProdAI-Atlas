"""Epic 16 A2A 3종 — 스트리밍·Push·확장 카드·skill 보안(SRV) + 클라이언트 위임·서명 카드 검증·추적·취소·재개(CLI)."""
import asyncio
import base64
import json
import os
import tempfile

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from atlas_agent_core import a2a_client, push
from atlas_agent_core.server import create_app
from atlas_agent_core.spec import AgentSpec, InputRequired, Skill
from atlas_agent_core.tasks import Base

MANIFEST = {"apiVersion": "agents.prodai.io/v1alpha1", "kind": "AtlasAgent",
            "metadata": {"name": "probe", "unitId": "MA-RES", "layer": "L1", "concept": "03-multi-agent"},
            "spec": {"version": "0.1.0", "interfaces": [{"binding": "JSONRPC", "port": 8000, "path": "/a2a"}]}}


async def slow(ctx):
    await asyncio.sleep(float(ctx.data.get("sleep", 3)))
    return {"slept": True}


async def ask(ctx):
    if "answer" not in ctx.data:
        raise InputRequired("답이 필요함")
    return {"answer": ctx.data["answer"]}


async def echo(ctx):
    return {"echo": ctx.data or ctx.text}


SPEC = AgentSpec(manifest=MANIFEST, description="A2A M2 시험 에이전트", skills=[
    Skill("echo", "echo", "되돌려준다 research", echo), Skill("slow", "slow", "느림", slow), Skill("ask", "ask", "입력 요구", ask),
    Skill("secure", "secure", "토큰 필요", echo, security="required")])


@pytest.fixture(scope="module")
def agent():
    d = tempfile.mkdtemp()
    eng = create_engine(f"sqlite:///{d}/a.db")
    Base.metadata.create_all(eng)
    os.environ["ATLAS_PUSH_ALLOW_HOSTS"] = "hooks.test"
    os.environ["ATLAS_PUSH_KEY_FILE"] = f"{d}/push.pem"
    app = create_app(SPEC, engine=eng, public_url="http://probe:8000")
    with TestClient(app) as c:
        c.engine = eng
        yield c


def rpc(c, method, params, headers=None):
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=headers or {}).json()


def msg(data=None, text=None, task_id=None):
    m = {"role": "ROLE_USER", "parts": [{"data": data}] if data is not None else [{"text": text or "x"}]}
    if task_id:
        m["taskId"] = task_id
    return m


# ---- 서버 (SRV-03·05, CARD-01) ----

def test_card_advertises_streaming_push_security(agent):
    card = agent.get("/.well-known/agent-card.json").json()
    assert card["capabilities"] == {"streaming": True, "pushNotifications": True, "extendedAgentCard": True}
    assert "atlasTenantJwt" in card["securitySchemes"]
    sec = next(s for s in card["skills"] if s["id"] == "secure")
    assert sec["securityRequirements"] == [{"atlasTenantJwt": []}]
    assert agent.get("/.well-known/jwks.json").json()["keys"][0]["crv"] == "P-256"


def test_skill_security_required_and_extended_card(agent):
    out = rpc(agent, "SendMessage", {"message": msg({"a": 1}), "metadata": {"skill": "secure"}})
    assert out["error"]["code"] == -32051
    assert rpc(agent, "GetExtendedAgentCard", {})["error"]["code"] == -32051
    assert rpc(agent, "SendMessage", {"message": msg({"a": 1}), "metadata": {"skill": "echo"}})["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_streaming_send_and_subscribe(agent):
    res = agent.post("/a2a", json={"jsonrpc": "2.0", "id": 7, "method": "SendStreamingMessage",
                                   "params": {"message": msg({"sleep": 0.2}), "metadata": {"skill": "slow"}}})
    assert res.headers["content-type"].startswith("text/event-stream")
    evs = [json.loads(ln[6:])["result"] for ln in res.text.splitlines() if ln.startswith("data: ")]
    assert "task" in evs[0] and "artifactUpdate" in evs[-2]
    assert evs[-1]["statusUpdate"]["final"] and evs[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    done = evs[0]["task"]["id"]
    sub = agent.post("/a2a", json={"jsonrpc": "2.0", "id": 8, "method": "SubscribeToTask", "params": {"id": done}})
    assert json.loads(sub.text.split("data: ", 1)[1])["error"]["code"] == -32004  # 종료 Task 구독 불가


def test_push_config_crud_ssrf_and_signed_delivery(agent):
    tid = rpc(agent, "SendMessage", {"message": msg({}), "metadata": {"skill": "ask"}})["result"]["task"]["id"]
    bad = rpc(agent, "CreateTaskPushNotificationConfig", {"taskId": tid, "pushNotificationConfig": {"url": "http://169.254.169.254/latest"}})
    assert bad["error"]["code"] == -32602 and "webhook 거부" in bad["error"]["message"]
    ok = rpc(agent, "CreateTaskPushNotificationConfig", {"taskId": tid, "pushNotificationConfig": {"url": "http://hooks.test/cb", "token": "t0k"}})["result"]
    cid = ok["pushNotificationConfig"]["id"]
    assert rpc(agent, "ListTaskPushNotificationConfig", {"taskId": tid})["result"]["configs"][0]["pushNotificationConfig"]["url"] == "http://hooks.test/cb"
    got = []

    def hook(req: httpx.Request):
        got.append((json.loads(req.content), req.headers))
        return httpx.Response(200)
    # 같은 키 파일을 쓰는 디스패처로 실제 발신 경로를 시험
    store = push.PushStore(agent.engine)
    signer = push.Signer(os.environ["ATLAS_PUSH_KEY_FILE"])
    d = push.Dispatcher(store, signer, "http://probe:8000", transport=httpx.MockTransport(hook))

    async def go():
        d.notify("default", {"statusUpdate": {"taskId": tid, "final": True}}, tid)
        await asyncio.sleep(0.2)
    asyncio.run(go())
    body, hdr = got[0]
    claims = jwt.decode(hdr["authorization"][7:], signer.key.public_key(), algorithms=["ES256"], audience="http://hooks.test/cb")
    assert body["statusUpdate"]["taskId"] == tid and claims["task_id"] == tid and hdr["x-a2a-notification-token"] == "t0k"
    assert rpc(agent, "DeleteTaskPushNotificationConfig", {"taskId": tid, "configId": cid}) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_push_retries_with_backoff(monkeypatch):
    monkeypatch.setenv("ATLAS_PUSH_BASE_DELAY_S", "0.01")
    monkeypatch.setenv("ATLAS_PUSH_MAX_RETRIES", "3")
    n = {"c": 0}

    def flaky(req):
        n["c"] += 1
        return httpx.Response(503 if n["c"] < 3 else 200)
    d = push.Dispatcher(None, push.Signer(tempfile.mktemp()), "iss", transport=httpx.MockTransport(flaky))
    cfg = push.PushConfigRow(tenant="t", task_id="x", id="1", url="http://hooks.test/a", token=None)
    assert asyncio.run(d._send(cfg, {})) is True and n["c"] == 3


# ---- 클라이언트 (CLI-01·02·04·06·07) — 가짜 Manager: 레지스트리 · 서명 카드 · JWKS · agent-proxy ----

def fake_manager(agent, tamper=False, calls=None):
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    b = lambda n: base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()
    jwks = {"keys": [{"kty": "EC", "crv": "P-256", "alg": "ES256", "kid": "k1", "x": b(nums.x), "y": b(nums.y)}]}

    def handle(req: httpx.Request):
        p = req.url.path
        (calls if calls is not None else []).append(p)
        if p == "/api/v1/a2a/registry/agents":
            return httpx.Response(200, json={"items": [{"agentId": "a1", "name": "probe", "skills": [{"id": "echo"}]}]})
        if p == "/.well-known/jwks.json":
            return httpx.Response(200, json=jwks)
        if p == "/api/v1/agents/a1/card":
            card = agent.get("/.well-known/agent-card.json").json()
            card["metadata"]["atlas.agentId"] = "a1"
            payload = a2a_client.canonical(card)
            sig = jwt.PyJWS().encode(payload, key, algorithm="ES256", headers={"kid": "k1"}).split(".")
            card["signatures"] = [{"protected": sig[0], "signature": sig[2]}]
            if tamper:
                card["description"] = "변조"
            return httpx.Response(200, json=card)
        if p == "/api/v1/agents/a1/a2a":
            res = agent.post("/a2a", content=req.content, headers={"content-type": "application/json"})
            return httpx.Response(res.status_code, content=res.content, headers={"content-type": res.headers["content-type"]})
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def client(agent, **kw):
    return a2a_client.A2AClient("http://manager:8080", transport=fake_manager(agent, **kw), engine=agent.engine)


def test_delegate_blocking_verified_and_mirrored(agent):
    out = asyncio.run(client(agent).delegate({"q": "x"}, skill="echo", parent="run-1"))
    assert out["state"] == "TASK_STATE_COMPLETED" and out["output"] == {"echo": {"q": "x"}} and out["agent"]["id"] == "a1"
    from sqlalchemy.orm import Session
    with Session(agent.engine) as s:
        row = s.get(push.RemoteTaskRow, ("default", out["taskId"]))
    assert row.state == "TASK_STATE_COMPLETED" and row.parent == "run-1" and row.skill == "echo"


def test_untrusted_card_blocks_delegation(agent):
    calls = []
    out = asyncio.run(a2a_client.A2AClient("http://m", transport=fake_manager(agent, tamper=True, calls=calls)).delegate("x", skill="echo"))
    assert out["state"] == "TASK_STATE_REJECTED" and out["reason"] == "untrusted_card"
    assert "/api/v1/agents/a1/a2a" not in calls


def test_stream_mode_and_depth_limit(agent):
    out = asyncio.run(client(agent).delegate({"sleep": 0.1}, skill="slow", mode="stream"))
    assert out["state"] == "TASK_STATE_COMPLETED" and out["output"] == {"slept": True}
    with pytest.raises(a2a_client.DepthExceeded):
        asyncio.run(client(agent).delegate("x", skill="echo", depth=2))


def test_timeout_cancels_and_input_required_resume(agent):
    c = client(agent)
    out = asyncio.run(c.delegate({"sleep": 5}, skill="slow", timeout=0.6))
    assert out["state"] == "TASK_STATE_CANCELED" and out["reason"] == "timeout"
    ask = asyncio.run(c.delegate({}, skill="ask"))
    assert ask["state"] == "TASK_STATE_INPUT_REQUIRED" and ask["reason"] == "input_required" and "답이 필요함" in ask["message"]
    done = asyncio.run(c.resume("a1", ask["taskId"], {"answer": 42}))
    assert done["state"] == "TASK_STATE_COMPLETED" and done["output"] == {"answer": 42}
