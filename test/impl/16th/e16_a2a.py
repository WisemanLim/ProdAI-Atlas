"""Epic 16 A2A 3종 스택 시험 — SDK A2AClient 로 실제 Manager·에이전트를 호출한다 (local·prod 공용).
실행: cd agents/runtime && uv run --frozen python ../../test/impl/16th/e16_a2a.py <local|prod> [push_host]
oidc 모드는 ATLAS_TEST_TOKEN(게이트 API 키)으로 Manager 에 인증. push 수신기는 호스트에서 띄우고 에이전트가 push_host 로 닿는다."""
import asyncio
import copy
import http.server
import json
import socket
import sys
import threading
import time
import urllib.request

import httpx
import jwt

from atlas_agent_core import a2a_client

ENV = sys.argv[1]
PUSH_HOST = sys.argv[2] if len(sys.argv) > 2 else "host.docker.internal"
M = "http://127.0.0.1:8080"
TOKEN = __import__("os").environ.get("ATLAS_TEST_TOKEN", "")
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


async def tok():
    return TOKEN or None


cl = a2a_client.A2AClient(M, tok)


async def raw_rpc(agent_id, method, params):
    return await cl._rpc(agent_id, method, params)


# ---- push 수신기 (호스트) ----
got_push = []


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        got_push.append((body, dict(self.headers)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


sock = socket.socket()
sock.bind(("0.0.0.0", 0))
PORT = sock.getsockname()[1]
sock.close()
srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Hook)
threading.Thread(target=srv.serve_forever, daemon=True).start()


async def main():
    agents = {a["name"]: a for a in await cl.search(limit=50)}
    plan, run = agents["al-plan"], agents["al-run"]

    # E16-01 서명 카드: Manager(Go JCS+JWS) → SDK(Python) 검증이 모든 ready 에이전트에서 통과, 변조 카드 거부
    verified = []
    for name, a in agents.items():
        try:
            c = await cl.card(a["agentId"])
            verified.append(c["metadata"]["atlas.agentId"] == a["agentId"])
        except a2a_client.Untrusted as exc:
            print("untrusted", name, exc)
            verified.append(False)
    card = await cl.card(plan["agentId"])
    bad = copy.deepcopy(card)
    bad["description"] += " (변조)"
    real = cl.card

    async def tampered(_):  # 검증 경로 재사용: 서명은 그대로, 본문만 바꾼 카드
        payload = a2a_client.canonical(bad)
        s = bad["signatures"][0]
        compact = f"{s['protected']}.{a2a_client._b64(payload)}.{s['signature']}"
        kid = json.loads(__import__("base64").urlsafe_b64decode(s["protected"] + "=="))["kid"]
        jwt.PyJWS().decode(compact, await cl._jwk(kid), algorithms=["ES256"])
    try:
        await tampered(None)
        tamper_rejected = False
    except jwt.PyJWTError:
        tamper_rejected = True
    cl.card = real
    check("E16-01", (len(verified) >= 6, all(verified), card["capabilities"]["streaming"], card["capabilities"]["pushNotifications"],
                     "atlasTenantJwt" in card["securitySchemes"], tamper_rejected), (True, True, True, True, True, True))

    # E16-02 skill 검색 레지스트리 (BM25 + 필터)
    top = await cl.search(q="계획 plan 분해", limit=3)
    by_skill = await cl.search(skill="loop.run")
    check("E16-02", (top[0]["name"] if top else None, [a["name"] for a in by_skill], all(a["trust"] == "signed" for a in agents.values())),
          ("al-plan", ["al-run"], True))

    # E16-03 원격 위임 blocking (skill 만 주고) → al-plan
    r = await cl.delegate("요구사항 정리. 그리고 구현. 그다음 테스트", skill="loop.plan")
    check("E16-03", (r["state"], r["agent"]["name"], len((r["output"] or {}).get("steps", []))), ("TASK_STATE_COMPLETED", "al-plan", 3))

    # E16-04 스트림 모드 — Manager agent-proxy 가 SSE 를 그대로 중계
    s = await cl.delegate({"goal": 'atlas_calc({"expr":"6*7"}) 로 계산', "dod": [{"type": "test", "spec": "contains:42"}]}, agent=run["agentId"], mode="stream", timeout=60)
    check("E16-04", (s["state"], "42" in json.dumps(s["output"])), ("TASK_STATE_COMPLETED", True))

    # E16-05 async 제출 → 추적(폴링)
    a = await cl.delegate({"goal": "atlas_echo 로 확인"}, agent=run["agentId"], mode="async")
    task = a
    for _ in range(60):
        t = await cl.get(run["agentId"], a["taskId"])
        if t["status"]["state"] in a2a_client.TERMINAL:
            task = t
            break
        await asyncio.sleep(0.5)
    check("E16-05", (a["reason"], task["status"]["state"] if isinstance(task, dict) and "status" in task else None), ("submitted", "TASK_STATE_COMPLETED"))

    # E16-06 INPUT_REQUIRED 되묻기 → 같은 taskId 로 재개 (HITL 승인)
    h = await cl.delegate({"goal": "atlas_delete_all 로 저장소를 정리한다"}, agent=run["agentId"], timeout=60)
    h2 = await cl.resume(run["agentId"], h["taskId"], {"approval": "approve"}) if h["state"] == "TASK_STATE_INPUT_REQUIRED" else {}
    check("E16-06", (h["state"], h2.get("state"), (h2.get("output") or {}).get("reason")), ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED", "END_TURN"))

    # E16-07 마감 초과 → CancelTask → 구조화 실패
    c = await cl.delegate({"goal": "never-finish atlas_echo", "budget": {"max_turns": 25}}, agent=run["agentId"], timeout=0.05)  # mock 루프는 수백 ms — 그보다 짧은 마감
    check("E16-07", (c["state"], c["reason"]), ("TASK_STATE_CANCELED", "timeout"))

    # E16-08 Push: webhook 등록 → 완료 시 서명 JWT(aud=URL) 와 함께 수신, 에이전트 JWKS 로 검증
    url = f"http://{PUSH_HOST}:{PORT}/cb"
    p0 = await cl.delegate({"goal": "atlas_echo 로 확인 한 번 더"}, agent=run["agentId"], mode="async")
    reg = await raw_rpc(run["agentId"], "CreateTaskPushNotificationConfig", {"taskId": p0["taskId"], "pushNotificationConfig": {"url": url, "token": "e16"}})
    for _ in range(60):
        if any((b.get("statusUpdate") or {}).get("final") for b, _ in got_push):
            break
        await asyncio.sleep(0.5)
    finals = [(b, h) for b, h in got_push if (b.get("statusUpdate") or {}).get("final")]
    ok_jwt = False
    if finals:
        body, hdr = finals[-1]
        jw = jwt.PyJWKClient("http://127.0.0.1:8001/.well-known/jwks.json")
        k = jw.get_signing_key_from_jwt(hdr["Authorization"][7:])
        claims = jwt.decode(hdr["Authorization"][7:], k.key, algorithms=["ES256"], audience=url)
        ok_jwt = claims["task_id"] == p0["taskId"] and hdr.get("X-A2A-Notification-Token") == "e16" and body.get("task", {}).get("status", {}).get("state") == "TASK_STATE_COMPLETED"
    # 등록 시점보다 먼저 끝났다면 push 가 없을 수 있다 — 그 경우 장기 작업으로 재시도
    check("E16-08", ("result" in reg, bool(finals), ok_jwt), (True, True, True))

    # E16-09 SSRF 차단 — 메타데이터 주소·허용 목록 밖 http
    b1 = await raw_rpc(run["agentId"], "CreateTaskPushNotificationConfig", {"taskId": p0["taskId"], "pushNotificationConfig": {"url": "http://169.254.169.254/latest/meta-data"}})
    b2 = await raw_rpc(run["agentId"], "CreateTaskPushNotificationConfig", {"taskId": p0["taskId"], "pushNotificationConfig": {"url": "http://evil.example.com/x"}})
    check("E16-09", ((b1.get("error") or {}).get("code"), (b2.get("error") or {}).get("code")), (-32602, -32602))

    # E16-10 확장 카드: Manager 경유(테넌트 JWT) 허용, 에이전트 직접(토큰 없음) 거부
    ext = await raw_rpc(plan["agentId"], "GetExtendedAgentCard", {})
    direct = httpx.post("http://127.0.0.1:8000/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetExtendedAgentCard", "params": {}}).json()
    check("E16-10", ((ext.get("result") or {}).get("metadata", {}).get("atlas.extended"), (direct.get("error") or {}).get("code")), (True, -32051))

    # E16-11 중첩 깊이 한도
    try:
        await cl.delegate("x", skill="loop.plan", depth=2)
        dep = False
    except a2a_client.DepthExceeded:
        dep = True
    check("E16-11", dep, True)


t0 = time.time()
asyncio.run(main())
srv.shutdown()
print(f"elapsed={time.time() - t0:.1f}s")
sys.exit(1 if failed else 0)
