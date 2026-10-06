"""Epic 18 승인함·알림 스택 시험 — AL-RUN HITL → Manager 승인함 → 결정 → 같은 Task 재개, 이벤트 → 규칙 → webhook(HMAC)·email(Mailpit).
실행: cd agents/runtime && uv run --frozen python ../../test/impl/18th/e18_hitl_ntf.py <local|prod>
env: ATLAS_TEST_TOKEN(prod) · ATLAS_GATEWAY_ADMIN_TOKEN · E18_HOOK_HOST(기본 host.docker.internal)"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
M, G, MAIL = "http://127.0.0.1:8080", "http://127.0.0.1:8090", "http://127.0.0.1:8025"
TOKEN = os.environ.get("ATLAS_TEST_TOKEN", "")
GW_ADMIN = os.environ.get("ATLAS_GATEWAY_ADMIN_TOKEN", "")
HOOK_HOST = os.environ.get("E18_HOOK_HOST", "host.docker.internal")
H = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
c = httpx.Client(timeout=60, headers=H)
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


# ---- 호스트 webhook 수신기 (Manager 컨테이너 → host.docker.internal) ----
HOOKS: list[dict] = []
HOOK_SECRET = secrets.token_hex(8)


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        HOOKS.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = HTTPServer(("0.0.0.0", 0), Hook)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def verify_sig(h: dict, body: bytes) -> bool:
    sig = {k: v for k, v in (p.split("=", 1) for p in h.get("X-Atlas-Signature", "").split(","))}
    want = hmac.new(HOOK_SECRET.encode(), f"{sig.get('t')}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig.get("v1", ""))


def rpc(agent, method, params):
    r = c.post(f"{M}/api/v1/agents/{agent}/a2a", json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params})
    return r.json()


def send(agent, data, task_id=None):
    msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"data": data}], **({"taskId": task_id} if task_id else {})}
    return rpc(agent, "SendMessage", {"message": msg})["result"]["task"]


def wait_task(agent, tid, states, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = rpc(agent, "GetTask", {"id": tid}).get("result", {})
        if t.get("status", {}).get("state") in states:
            return t
        time.sleep(1)
    return t


def approvals(**q):
    return c.get(f"{M}/api/v1/approvals", params={"org": "default", **q}).json().get("items", [])


def approval_for(task_id, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        hit = [a for a in approvals(status="pending") if a["sourceRef"] == task_id]
        if hit:
            return hit[0]
        time.sleep(1)
    return {}


def main():
    ags = {a["name"]: a["agentId"] for a in c.get(f"{M}/api/v1/a2a/registry/agents", params={"limit": 50}).json()["items"]}
    run = ags["al-run"]

    # E18-01 루프 HITL → 통합 승인함 등록 (HITL-01): source=loop, sourceRef=A2A taskId, 위험·만료·payload 해시
    ta = send(run, {"goal": "atlas_delete_all 로 정리"})
    ap = approval_for(ta["id"])
    pl = json.loads(ap.get("payload") or "{}")
    check("E18-01", (ta["status"]["state"], ap.get("source"), ap.get("unitId"), ap.get("riskLevel"), "atlas_delete_all" in ap.get("action", ""),
                     bool(pl.get("calls")), len(ap.get("payloadHash", "")), ap.get("agentId") == run),
          ("TASK_STATE_INPUT_REQUIRED", "loop", "AL-RUN", "high", True, True, 64, True))

    # E18-02 정렬·필터 (HITL-02): 위험도 → 만료 임박순
    mine = []
    for risk, ttl in (("low", 600), ("critical", 3600), ("high", 7200), ("high", 300)):
        r = c.post(f"{M}/api/v1/approvals", params={"org": "default"}, json={"source": "tool", "sourceRef": f"sort-{uuid.uuid4().hex[:6]}", "agentId": run,
                                                                           "action": "deploy.prod", "riskLevel": risk, "payload": {"n": risk}, "expiresInS": ttl})
        mine.append(r.json()["id"])
    order = [a["id"] for a in approvals(status="pending") if a["id"] in mine]
    crit_only = {a["riskLevel"] for a in approvals(status="pending", risk="critical")}
    check("E18-02", (order, crit_only), ([mine[1], mine[3], mine[2], mine[0]], {"critical"}))

    # E18-03 수정 후 승인 → 같은 Task 재개 (HITL-01) + 감사 (payload 해시)
    d = c.post(f"{M}/api/v1/approvals/{ap['id']}/decision", params={"org": "default"},
               json={"decision": "edit", "reason": "범위 축소", "payload": {"arguments": {"scope": "tmp-only"}}}).json()
    done = wait_task(run, ta["id"], {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"})
    ev = httpx.get(f"{M}/svc/alrun/runs/{ta['id']}/events", params={"tenant": "default"}, headers=H, timeout=10).text
    again = c.post(f"{M}/api/v1/approvals/{ap['id']}/decision", params={"org": "default"}, json={"decision": "approve"})
    check("E18-03", (d.get("status"), done.get("status", {}).get("state"), "event: hitl_edit" in ev, again.status_code),
          ("edited", "TASK_STATE_COMPLETED", True, 409))

    # E18-04 반려 → 루프 HITL_DENIED 종료, 사유 기록 (HITL-07)
    tb = send(run, {"goal": "atlas_delete_all 로 정리"})
    bp = approval_for(tb["id"])
    rj = c.post(f"{M}/api/v1/approvals/{bp['id']}/decision", params={"org": "default"}, json={"decision": "reject", "reason": "운영 데이터 삭제 금지"}).json()
    end = wait_task(run, tb["id"], {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"})
    res = ((end.get("artifacts") or [{}])[0].get("parts") or [{}])[0].get("data", {})
    det = c.get(f"{M}/api/v1/approvals/{bp['id']}", params={"org": "default"}).json()["approval"]
    check("E18-04", (rj.get("status"), res.get("reason") or end.get("status", {}).get("state"), det.get("decisionReason")),
          ("rejected", "HITL_DENIED", "운영 데이터 삭제 금지"))

    # E18-05 감사: approval.request·decide 체인 검증 (HITL-01, PLT-TEN 감사)
    au = c.get(f"{M}/api/v1/orgs/default/audit", params={"action": "approval.decide"}).json().get("items", [])
    ver = c.get(f"{M}/api/v1/orgs/default/audit/verify").json()
    hashes = any(ap["payloadHash"] in (x.get("detail") or "") for x in au)   # before/after 는 해시만 — payload 해시는 detail 에
    check("E18-05", (len(au) >= 2, hashes, ver.get("valid")), (True, True, True))

    # E18-06 알림 규칙: SSRF 거부 · 서명 키 가림 (NTF-01)
    # 규칙은 default 조직(시험 주체 = default 관리자), 예산은 이 실행의 가상키 범위 — budget_id 조건으로 다른 이벤트와 격리
    org, tag = "default", "ntf-" + secrets.token_hex(3)
    rules = []
    hook_url = f"http://{HOOK_HOST}:{PORT}/hook"
    bad = c.post(f"{M}/api/v1/orgs/{org}/notifications/rules", json={"name": "x", "topic": "gw.>", "channels": [{"type": "webhook", "url": "http://169.254.169.254/latest"}]})
    r1 = c.post(f"{M}/api/v1/orgs/{org}/notifications/rules", json={"name": "예산 80%", "topic": "gw.budget.threshold", "severity": "warning", "throttleS": 300,
                                                                   "filter": [{"field": "threshold", "op": ">=", "value": 80}, {"field": "budget_id", "op": "==", "value": f"{tag}-key"}],
                                                                   "channels": [{"type": "webhook", "url": hook_url, "secret": HOOK_SECRET}]}).json()
    to = f"{tag}@example.test"
    r2 = c.post(f"{M}/api/v1/orgs/{org}/notifications/rules", json={"name": "예산 소진", "topic": "gw.budget.threshold", "severity": "critical",
                                                                   "filter": [{"field": "threshold", "op": "==", "value": 100}, {"field": "budget_id", "op": "==", "value": f"{tag}-key"}],
                                                                   "channels": [{"type": "email", "to": [to]}]}).json()
    rules += [r1.get("id"), r2.get("id")]
    check("E18-06", (bad.status_code, bad.json().get("error"), r1.get("channels", [{}])[0].get("secret")), (400, "unsafe_url", "***"))

    # E18-07 예산 임계 → webhook(HMAC) · email(Mailpit) · 스로틀 (NTF-02·03)
    k = httpx.post(f"{G}/admin/keys", json={"tenant": org, "project": "p1", "models": ["*"], "rpm": 0, "tpm": 0}, headers={"Authorization": f"Bearer {GW_ADMIN}"}).json()
    httpx.put(f"{G}/admin/budgets/{tag}-key", json={"scope": "key", "scope_id": k["virtualKey"]["prefix"], "amount_usd": "0.00001", "period": "monthly", "hard_action": "block"},
              headers={"Authorization": f"Bearer {GW_ADMIN}"})
    httpx.post(f"{G}/v1/chat/completions", json={"model": "chat-large", "messages": [{"role": "user", "content": "예산 소모 " * 60}]},
               headers={"Authorization": f"Bearer {k['key']}", "x-atlas-route-pin": "true"}, timeout=30)
    t0 = time.time()
    mails = []
    while time.time() - t0 < 30 and not (HOOKS and mails):
        time.sleep(1)
        mails = httpx.get(f"{MAIL}/api/v1/search", params={"query": f"to:{to}"}, timeout=10).json().get("messages", [])
    dl = [d for r in rules for d in c.get(f"{M}/api/v1/orgs/{org}/notifications/deliveries", params={"rule": r}).json().get("items", [])]
    st = sorted(f"{d['channel']}:{d['status']}" for d in dl)
    hb = json.loads(HOOKS[0]["body"]) if HOOKS else {}
    check("E18-07", (len(HOOKS), bool(HOOKS) and verify_sig(HOOKS[0]["headers"], HOOKS[0]["body"]), hb.get("data", {}).get("threshold"),
                     HOOKS[0]["headers"].get("X-Atlas-Event") if HOOKS else None, len(mails), "CRITICAL" in (mails[0]["Subject"] if mails else ""), st),
          (1, True, 80, "gw.budget.threshold", 1, True, ["-:throttled", "email:sent", "webhook:sent"]))
    httpx.delete(f"{G}/admin/budgets/{tag}-key", headers={"Authorization": f"Bearer {GW_ADMIN}"})

    # E18-08 쿼터 초과 → 알림 (NTF-03) · 시험 발송
    rq = c.post(f"{M}/api/v1/orgs/default/notifications/rules", json={"name": f"쿼터 {tag}", "topic": "plt.quota.exceeded", "channels": [{"type": "webhook", "url": hook_url + "/quota"}]}).json()
    rules.append(rq.get("id"))
    proj = "q" + secrets.token_hex(3)
    c.post(f"{M}/api/v1/orgs/default/projects", json={"slug": proj, "name": proj})
    c.put(f"{M}/api/v1/orgs/default/quotas", json={"scope": "project", "project": proj, "key": "agents.max", "limit": 0})
    man = json.load(open(os.path.join(os.path.dirname(__file__), "../../../agents/runtime/units/al_plan/atlas-agent.json")))
    man["metadata"]["name"] = "quota-probe"
    qr = c.post(f"{M}/api/v1/agents", params={"project": proj}, json=man)
    t0 = time.time()
    while time.time() - t0 < 15 and not any(h["path"].endswith("/quota") for h in HOOKS):
        time.sleep(1)
    qh = [json.loads(h["body"]) for h in HOOKS if h["path"].endswith("/quota")]
    rid = r1["id"]
    tst = c.post(f"{M}/api/v1/orgs/{org}/notifications/rules/{rid}/test").json().get("deliveries", [])
    check("E18-08", (qr.status_code, bool(qh) and qh[-1]["data"].get("key"), [d["status"] for d in tst]), (429, "agents.max", ["test:sent"]))

    # E18-09 만료 → expired + 요청 에이전트에 deny (HITL-01)
    tc = send(run, {"goal": "atlas_delete_all 로 정리"})
    auto = approval_for(tc["id"])
    ex = c.post(f"{M}/api/v1/approvals", params={"org": "default"}, json={"source": "loop", "sourceRef": tc["id"], "agentId": run, "action": "atlas_delete_all",
                                                                        "riskLevel": "medium", "payload": {}, "expiresInS": 60}).json()
    t0 = time.time()
    while time.time() - t0 < 110:
        cur = c.get(f"{M}/api/v1/approvals/{ex['id']}", params={"org": "default"}).json()["approval"]
        if cur["status"] != "pending":
            break
        time.sleep(3)
    endc = wait_task(run, tc["id"], {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}, 20)
    resc = ((endc.get("artifacts") or [{}])[0].get("parts") or [{}])[0].get("data", {})
    check("E18-09", (cur["status"], cur.get("callbackResult") not in (None, "callback_error", "rpc_error"), resc.get("reason")), ("expired", True, "HITL_DENIED"))

    # E18-11 승인함 밖(같은 Task 직접 응답)에서 재개 → 대기 요청 철회 (withdrawn)
    td = send(run, {"goal": "atlas_delete_all 로 정리"})
    dp = approval_for(td["id"])
    send(run, {"approval": "approve"}, task_id=td["id"])
    endd = wait_task(run, td["id"], {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"})
    dd = c.get(f"{M}/api/v1/approvals/{dp.get('id')}", params={"org": "default"}).json().get("approval", {})
    check("E18-11", (endd.get("status", {}).get("state"), dd.get("status"), dd.get("callbackResult")), ("TASK_STATE_COMPLETED", "withdrawn", "resolved_in_task"))

    # 정리: 시험 규칙 삭제 · 남은 시험 요청 반려
    for r in rules:
        if r:
            c.delete(f"{M}/api/v1/orgs/{org}/notifications/rules/{r}")
    for i in [*mine, auto.get("id")]:
        if i:
            c.post(f"{M}/api/v1/approvals/{i}/decision", params={"org": "default"}, json={"decision": "reject", "reason": "e18 정리"})

    # E18-10 권한: 읽기 전용 주체는 결정 불가 (prod 만 — local 은 인증 off)
    if MODE == "prod":
        ro = c.post(f"{M}/api/v1/orgs/default/apikeys", json={"name": "e18-ro", "scopes": "read", "expiresInDays": 1}).json()
        x = httpx.post(f"{M}/api/v1/approvals/{mine[0]}/decision", params={"org": "default"}, json={"decision": "approve"},
                       headers={"Authorization": f"Bearer {ro['key']}"})
        lst = httpx.get(f"{M}/api/v1/approvals", params={"org": "beta-none"}, headers={"Authorization": f"Bearer {ro['key']}"})
        c.delete(f"{M}/api/v1/orgs/default/apikeys/{ro['apiKey']['id']}")
        check("E18-10", (x.status_code, lst.status_code), (403, 403))


main()
srv.shutdown()
print("== failed", failed)
sys.exit(1 if failed else 0)
