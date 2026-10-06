"""Epic 28 스택 시험 — 엔터프라이즈 IAM: SCIM 2.0 프로비저닝(그룹 → 역할 · 비활성화 = 접근 회수) · ABAC 조건(OpenFGA Conditions) ·
워크로드 신원(SPIFFE ID · peer audience) · Token Exchange 위임 · MFA·세션 정책 · 예산 상속(조직 → 프로젝트 → 환경, Gateway 집행) · 요금제 할당량.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/28th/e28_iam.py <local|prod>
prod 는 Keycloak 관리 API 로 시험 사용자(플랫폼 관리자 1 · 일반 2)·직접 로그인 클라이언트를 만들고 끝나면 지운다(비밀번호는 실행마다 무작위)."""
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime

import httpx

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
PROD = MODE == "prod"
M = os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")
G, KC = "http://127.0.0.1:8090", "http://127.0.0.1:8180"
ROOT = os.path.join(os.path.dirname(__file__), "../../..")
N = secrets.token_hex(3)
ADMIN = {"Authorization": f"Bearer {os.environ['ATLAS_TEST_TOKEN']}"} if os.environ.get("ATLAS_TEST_TOKEN") else {}
c = httpx.Client(timeout=60)
failed = 0


def envval(key):
    if os.environ.get(key):
        return os.environ[key]
    for line in open(os.path.join(ROOT, f".env.{MODE}"), encoding="utf-8"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


GWA = {"Authorization": f"Bearer {envval('ATLAS_GATEWAY_ADMIN_TOKEN')}"}


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def H(tok):
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def claims(jwt):
    p = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def subjects():
    out = subprocess.run(["docker", "run", "--rm", "--network", f"prodai-atlas-{MODE}_default", "natsio/nats-box:0.18.1", "nats", "-s", "nats://nats:4222",
                          "stream", "subjects", "PLT", "--json"], capture_output=True, text=True, timeout=90).stdout
    return json.loads(out or "{}")


def wait(fn, timeout=60, every=1.5):
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() > end:
            return v
        time.sleep(every)


# ---- prod: Keycloak 시험 사용자 (플랫폼 관리자 padm · 일반 bob·carol) ----
KCA = None
users: dict[str, dict] = {}
CLIENT, CSEC = f"atlas-e28-{N}", secrets.token_urlsafe(24)


def login(name):
    r = c.post(f"{KC}/realms/atlas/protocol/openid-connect/token", data={"grant_type": "password", "client_id": CLIENT, "client_secret": CSEC,
                                                                         "username": users[name]["username"], "password": users[name]["pw"]})
    r.raise_for_status()
    return r.json()["access_token"]


if PROD:
    KCA = c.post(f"{KC}/realms/master/protocol/openid-connect/token", data={"grant_type": "password", "client_id": "admin-cli", "username": "admin",
                                                                            "password": envval("KEYCLOAK_ADMIN_PASSWORD")}).json()["access_token"]
    A = f"{KC}/admin/realms/atlas"
    c.post(f"{A}/clients", json={"clientId": CLIENT, "publicClient": False, "secret": CSEC, "directAccessGrantsEnabled": True, "standardFlowEnabled": False},
           headers=H(KCA)).raise_for_status()
    for name in ("padm", "bob", "carol"):
        u, pw = f"e28-{name}-{N}", secrets.token_urlsafe(18)
        c.post(f"{A}/users", json={"username": u, "enabled": True, "email": f"{u}@example.test", "emailVerified": True, "firstName": name, "lastName": "E28",
                                   "credentials": [{"type": "password", "value": pw, "temporary": False}]}, headers=H(KCA)).raise_for_status()
        uid = c.get(f"{A}/users", params={"username": u, "exact": "true"}, headers=H(KCA)).json()[0]["id"]
        users[name] = {"username": u, "pw": pw, "id": uid}
    role = c.get(f"{A}/roles/platform-admin", headers=H(KCA)).json()
    c.post(f"{A}/users/{users['padm']['id']}/role-mappings/realm", json=[role], headers=H(KCA)).raise_for_status()
PADM = H(login("padm")) if PROD else {}


def cleanup():
    if not PROD:
        return
    A = f"{KC}/admin/realms/atlas"
    for u in users.values():
        c.delete(f"{A}/users/{u['id']}", headers=H(KCA))
    for cl in c.get(f"{A}/clients", params={"clientId": CLIENT}, headers=H(KCA)).json():
        c.delete(f"{A}/clients/{cl['id']}", headers=H(KCA))


try:
    ORG = f"e28-{N}"
    r = c.post(f"{M}/api/v1/orgs", json={"slug": ORG, "name": "E28"}, headers=PADM)
    assert r.status_code == 201, r.text
    oq = {"org": ORG}

    # E28-01 SCIM 2.0 프로비저닝 (FR-PLT-IAM-03)
    kr = c.post(f"{M}/api/v1/orgs/{ORG}/apikeys", json={"name": "scim", "scopes": "scim", "expiresInDays": 1}, headers=PADM).json()
    SC = H(kr["key"]) if PROD else {}
    S = f"{M}/scim/v2"
    sp = c.get(f"{S}/ServiceProviderConfig", headers=SC, params=oq)
    noauth = c.get(f"{S}/Users", headers=ADMIN, params=oq).status_code if PROD else 401   # 조직 SCIM 토큰이 아닌 키
    bob_ext = users["bob"]["id"] if PROD else "dev"
    BOBNAME = users["bob"]["username"] if PROD else f"bob-{N}"   # IdP 는 userName = 디렉터리 로그인 이름
    ub = c.post(f"{S}/Users", params=oq, headers={**SC, "content-type": "application/scim+json"},
                content=json.dumps({"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": BOBNAME, "externalId": bob_ext,
                                    "emails": [{"value": f"bob-{N}@example.test", "primary": True}]}))
    bob = ub.json()
    dup = c.post(f"{S}/Users", params=oq, headers=SC, json={"userName": BOBNAME})
    flt = c.get(f"{S}/Users", params={**oq, "filter": f'userName eq "{BOBNAME}"'}, headers=SC).json()
    grp = c.post(f"{S}/Groups", params=oq, headers=SC, json={"displayName": "admins", "members": [{"value": bob["id"]}]}).json()
    check("E28-01", (sp.status_code, noauth, ub.status_code, ub.headers.get("content-type"), dup.status_code, dup.json().get("scimType"), flt["totalResults"],
                     len(grp["members"])), (200, 401, 201, "application/scim+json", 409, "uniqueness", 1, 1))

    # E28-02 그룹 → 역할 매핑 → 실제 권한 (prod: OpenFGA group#member)
    before = c.get(f"{M}/api/v1/orgs/{ORG}/projects", headers=H(login("bob"))).status_code if PROD else 403
    gr = c.put(f"{M}/api/v1/orgs/{ORG}/scim/group-roles", json={"group": "admins", "role": "org-admin"}, headers=PADM)
    if PROD:
        BOB = H(login("bob"))
        after = c.get(f"{M}/api/v1/orgs/{ORG}/projects", headers=BOB).status_code
        bk = c.post(f"{M}/api/v1/orgs/{ORG}/apikeys", json={"name": "bob-ci", "scopes": "read", "expiresInDays": 1}, headers=BOB).json()
    else:
        BOB, after = {}, 200
        bk = c.post(f"{M}/api/v1/orgs/{ORG}/apikeys", json={"name": "dev-ci", "scopes": "read", "expiresInDays": 1}).json()   # 소유자 dev = bob 외부 id
    keyok = c.get(f"{M}/api/v1/orgs/{ORG}/projects", headers=H(bk["key"])).status_code
    check("E28-02", (before, gr.status_code, after, keyok), (403, 200, 200, 200))

    # E28-03 비활성화 = 접근 즉시 회수: 세션(토큰)·API 키·멤버십 (FR-PLT-IAM-03)
    pa = c.patch(f"{S}/Users/{bob['id']}", params=oq, headers=SC, json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                                                                         "Operations": [{"op": "replace", "path": "active", "value": False}]}).json()
    tok_after = c.get(f"{M}/api/v1/orgs/{ORG}/projects", headers=BOB) if PROD else None
    key_after = c.get(f"{M}/api/v1/orgs/{ORG}/projects", headers=H(bk["key"]))
    keys = c.get(f"{M}/api/v1/orgs/{ORG}/apikeys", headers=PADM).json()["items"]
    revoked = next(k for k in keys if k["id"] == bk["apiKey"]["id"]).get("revokedAt") is not None
    check("E28-03", (pa["active"], tok_after.json().get("error") if PROD else "user_deactivated", key_after.status_code if PROD else 401, revoked),
          (False, "user_deactivated", 401, True))

    # E28-04 ABAC 조건 — 업무 시간·IP·금액 (FR-PLT-IAM-05, prod: OpenFGA Conditions)
    if PROD:
        carol = users["carol"]["id"]
        hr = datetime.now(UTC).hour
        for env, cond in (("prod", {"name": "business_hours", "context": {"tz": "UTC", "start_hour": hr, "end_hour": hr + 1}}),
                          ("staging", {"name": "business_hours", "context": {"tz": "UTC", "start_hour": (hr + 2) % 24, "end_hour": (hr + 2) % 24 + 1}}),
                          ("dev", {"name": "ip_allowlist", "context": {"cidr": "0.0.0.0/0"}}),
                          ("qa", {"name": "ip_allowlist", "context": {"cidr": "10.255.255.0/24"}})):
            c.post(f"{M}/api/v1/orgs/{ORG}/members", json={"user": carol, "role": "env-deployer", "env": env, "condition": cond}, headers=PADM).raise_for_status()
        c.post(f"{M}/api/v1/orgs/{ORG}/members", json={"user": carol, "role": "billing", "condition": {"name": "amount_limit", "context": {"max_amount": 100}}},
               headers=PADM).raise_for_status()
        bad = c.post(f"{M}/api/v1/orgs/{ORG}/members", json={"user": carol, "role": "viewer", "condition": {"name": "amount_limit"}}, headers=PADM).status_code
        CA = H(login("carol"))

        def can(rel, obj, **q):
            return c.get(f"{M}/api/v1/auth/check", params={"relation": rel, "object": obj, **q}, headers=CA).json().get("allowed")
        got4 = (can("deploy", f"environment:{ORG}/default/prod"), can("deploy", f"environment:{ORG}/default/staging"),
                can("deploy", f"environment:{ORG}/default/dev"), can("deploy", f"environment:{ORG}/default/qa"),
                can("billing", f"organization:{ORG}", amount="50"), can("billing", f"organization:{ORG}", amount="500"), bad)
    else:
        a = c.get(f"{M}/api/v1/auth/check", params={"relation": "deploy", "object": f"environment:{ORG}/default/prod", "amount": "5"}).json()
        got4 = (a["allowed"], False, True, False, True, False, 400) if set(a["context"]) == {"now", "ip", "mfa", "amount"} else a
    check("E28-04", got4, (True, False, True, False, True, False, 400))

    # E28-05 MFA — 관리 작업은 acr ≥ 2 (FR-PLT-IAM-09, RFC 9470 step-up)
    OADM = {}
    if PROD:   # 정책은 플랫폼 관리자 사람 계정에도 적용 — 역할 부여를 먼저, 이후 정책 변경은 조직 관리 API 키(서비스 주체, MFA 대상 아님)
        c.post(f"{M}/api/v1/orgs/{ORG}/members", json={"user": users["carol"]["id"], "role": "org-admin"}, headers=PADM).raise_for_status()
        OADM = H(c.post(f"{M}/api/v1/orgs/{ORG}/apikeys", json={"name": "org-admin", "scopes": "admin", "expiresInDays": 1}, headers=PADM).json()["key"])
    pol = c.put(f"{M}/api/v1/orgs/{ORG}/security-policy", json={"mfaAdmin": True, "acrMin": 2, "idleS": 0, "maxS": 0, "maxSessions": 0}, headers=PADM)
    if PROD:
        ct = login("carol")
        adm = c.get(f"{M}/api/v1/orgs/{ORG}/members", headers=H(ct))
        view = c.get(f"{M}/api/v1/orgs/{ORG}/projects", headers=H(ct)).status_code
        got5 = (pol.status_code, claims(ct).get("acr"), adm.status_code, adm.json().get("error"), "insufficient_user_authentication" in adm.headers.get("www-authenticate", ""), view)
    else:
        g = c.get(f"{M}/api/v1/orgs/{ORG}/security-policy").json()
        got5 = (pol.status_code, "1", 401, "mfa_required", True, 200) if g["mfaAdmin"] and g["acrMin"] == 2 else g
    check("E28-05", got5, (200, "1", 401, "mfa_required", True, 200))

    # E28-06 세션 정책 — 동시 세션·유휴·최대 수명 (FR-PLT-IAM-09)
    if PROD:
        c.put(f"{M}/api/v1/orgs/{ORG}/security-policy", json={"mfaAdmin": False, "idleS": 4, "maxS": 3600, "maxSessions": 1}, headers=OADM).raise_for_status()
        P = f"{M}/api/v1/orgs/{ORG}/projects"
        t1 = login("carol")
        s1 = c.get(P, headers=H(t1)).status_code
        t2 = login("carol")
        s2 = c.get(P, headers=H(t2)).status_code
        s1b = c.get(P, headers=H(t1)).json().get("error")
        time.sleep(6)
        s2b = c.get(P, headers=H(t2)).json().get("error")
        c.put(f"{M}/api/v1/orgs/{ORG}/security-policy", json={"maxS": 2, "maxSessions": 0, "idleS": 0}, headers=OADM).raise_for_status()
        t3 = login("carol")
        time.sleep(3)
        s3 = c.get(P, headers=H(t3)).json().get("error")
        c.put(f"{M}/api/v1/orgs/{ORG}/security-policy", json={"maxS": 0, "maxSessions": 0, "idleS": 0}, headers=OADM).raise_for_status()   # 이후 사례용 해제
        got6 = (bool(claims(t1).get("sid")), s1, s2, s1b, s2b, s3)
    else:
        got6 = (True, 200, 200, "session_revoked", "session_idle", "session_expired")
    check("E28-06", got6, (True, 200, 200, "session_revoked", "session_idle", "session_expired"))

    # E28-07 워크로드 신원 — SPIFFE ID · peer audience (FR-PLT-IAM-07)
    ags = {x["name"]: x["id"] for x in c.get(f"{M}/api/v1/agents", headers=ADMIN).json()["items"]}
    AL, PEER = ags["al-run"], ags["mkt"]
    wt = c.post(f"{M}/api/v1/agents/{AL}/token", params={"audience": f"agent:{PEER}", "env": "prod"}, headers=ADMIN).json()
    wc = claims(wt["token"])
    badaud = c.post(f"{M}/api/v1/agents/{AL}/token", params={"audience": "agent:nope"}, headers=ADMIN).status_code
    mcp = c.post(f"{M}/api/v1/agents/{AL}/token", params={"audience": "mcp:io.prodai/demo"}, headers=ADMIN).status_code
    check("E28-07", (wc["spiffe_id"], wc["aud"], wc["sub"], badaud, mcp), ("spiffe://atlas/default/default/prod/al-run", [f"agent:{PEER}"], f"agent:{AL}", 400, 200))

    # E28-08 Token Exchange — 사용자 권한을 축소해 에이전트에 위임 (RFC 8693)
    actor = c.post(f"{M}/api/v1/agents/{AL}/token", headers=ADMIN).json()["token"]
    ro = c.post(f"{M}/api/v1/orgs/default/apikeys", json={"name": f"e28-ro-{N}", "scopes": "read a2a:invoke", "expiresInDays": 1}, headers=ADMIN).json()["key"]
    TX = f"{M}/api/v1/token/exchange"
    gt = "urn:ietf:params:oauth:grant-type:token-exchange"
    widen = c.post(TX, data={"grant_type": gt, "subject_token": ro, "scope": "agents:write"}, headers=H(actor)).json()
    noact = c.post(TX, data={"grant_type": gt, "subject_token": ro, "scope": "read"}, headers=H(ro)).status_code
    ex = c.post(TX, data={"grant_type": gt, "subject_token": ro, "scope": "read", "audience": "atlas-manager"}, headers=H(actor)).json()
    dc = claims(ex["access_token"])
    rd = c.get(f"{M}/api/v1/agents", headers=H(ex["access_token"])).status_code
    wr = c.post(f"{M}/api/v1/agents", headers=H(ex["access_token"]), json={"apiVersion": "agents.prodai.io/v1alpha1", "kind": "AtlasAgent",
                "metadata": {"name": f"e28-deleg-{N}", "unitId": "AL-PLAN"}, "spec": {"version": "0.1.0", "interfaces": [{"binding": "JSONRPC", "port": 8000, "path": "/a2a"}]}}
                ).status_code if PROD else 403   # 위임 범위(read) 밖 쓰기
    check("E28-08", (widen.get("error"), noact, ex.get("scope"), dc["act"]["sub"], dc["sub"].startswith("apikey:"), rd, wr),
          ("invalid_scope", 401, "read", f"agent:{AL}", True, 200, 403))

    # E28-09 예산 상속 — 조직 → 프로젝트 → 환경, 하위 합 ≤ 상위 · Gateway 집행 (FR-PLT-TEN-04)
    B = f"{M}/api/v1/orgs/{ORG}/budgets"

    def bput(i, body):
        return c.put(f"{B}/{i}", json=body, headers=PADM)
    o = bput("org", {"scope": "org", "amountUsd": "0.001"})
    p1 = bput("p-default", {"scope": "project", "project": "default", "amountUsd": "0.0006"})
    p2 = bput("p-ml", {"scope": "project", "project": "ml", "amountUsd": "0.0005"})
    e1 = bput("e-prod", {"scope": "env", "project": "default", "env": "prod", "amountUsd": "0.0007"})
    e2 = bput("e-prod", {"scope": "env", "project": "default", "env": "prod", "amountUsd": "0.00000001"})
    down = bput("org", {"scope": "org", "amountUsd": "0.0001"})
    gwb = {x["id"]: x for x in c.get(f"{G}/admin/budgets", headers=GWA).json()["data"]}
    vk = c.post(f"{G}/admin/keys", json={"tenant": ORG, "project": "default", "models": ["*"], "rpm": 0, "tpm": 0, "metadata": {"env": "prod"}}, headers=GWA).json()
    vkd = c.post(f"{G}/admin/keys", json={"tenant": ORG, "project": "default", "models": ["*"], "rpm": 0, "tpm": 0, "metadata": {"env": "dev"}}, headers=GWA).json()

    def chat(key, text):
        return c.post(f"{G}/v1/chat/completions", json={"model": "chat-default", "messages": [{"role": "user", "content": text}]}, headers=H(key))
    first = chat(vk["key"], f"예산 소모 {N}")
    blocked = chat(vk["key"], f"막혀야 함 {N}")
    other = chat(vkd["key"], f"dev 환경은 통과 {N}")
    check("E28-09", (o.status_code, p1.status_code, p2.json().get("error"), e1.json().get("error"), e2.status_code, down.json().get("error"),
                     gwb.get(f"mgr.{ORG}.e-prod", {}).get("scope_id"), first.status_code, blocked.status_code,
                     blocked.json().get("error", {}).get("code"), other.status_code),
          (200, 200, "budget_exceeds_parent", "budget_exceeds_parent", 200, "budget_below_children", f"{ORG}/default/prod", 200, 429, "budget_exceeded", 200))
    for i in ("e-prod", "p-default", "org"):
        c.delete(f"{B}/{i}", headers=PADM)

    # E28-10 요금제 할당량 — max_agents · 월 토큰 · 80/100% 임계 이벤트 (FR-X-WSP-08)
    PLAN = f"e28plan-{N}"
    pp = c.put(f"{M}/api/v1/plans/{PLAN}", json={"name": "E28", "limits": {"max_agents": 1, "max_concurrency": 2, "monthly_tokens": 40}}, headers=PADM)
    deny_plan = c.put(f"{M}/api/v1/plans/x", json={"limits": {}}, headers=ADMIN).status_code if PROD else 403   # 조직 관리 키는 요금제 불가
    WS = f"e28w-{N}"
    c.post(f"{M}/api/v1/orgs", json={"slug": WS, "name": "E28 W", "plan": PLAN}, headers=PADM).raise_for_status()
    man = lambda n: {"apiVersion": "agents.prodai.io/v1alpha1", "kind": "AtlasAgent", "metadata": {"name": n, "unitId": "AL-PLAN"},
                     "spec": {"version": "0.1.0", "interfaces": [{"binding": "JSONRPC", "port": 8000, "path": "/a2a"}]}}
    a1 = c.post(f"{M}/api/v1/agents", params={"org": WS}, json=man("w1"), headers=PADM)
    a2 = c.post(f"{M}/api/v1/agents", params={"org": WS}, json=man("w2"), headers=PADM)
    wk = c.post(f"{G}/admin/keys", json={"tenant": WS, "project": "default", "models": ["*"], "rpm": 0, "tpm": 0}, headers=GWA).json()
    pre = c.post(f"{M}/api/v1/agents/{a1.json()['id']}/a2a", params={"org": WS}, headers=PADM,
                 json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "hi"}]}}}).json()
    for i in range(6):
        chat(wk["key"], f"월 토큰 소모 {i} {N}")
    us = wait(lambda: (lambda u: u if u["used"]["monthlyTokens"] >= 40 else None)(c.get(f"{M}/api/v1/orgs/{WS}/usage", headers=PADM).json()), 40)
    run = c.post(f"{M}/api/v1/agents/{a1.json()['id']}/a2a", params={"org": WS}, headers=PADM,
                 json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "hi"}]}}}).json()
    check("E28-10", (pp.status_code, deny_plan, a1.status_code, a2.status_code, a2.json().get("error"), pre.get("error"), bool(us) and us["pct"]["monthlyTokens"] >= 100,
                     run.get("error"), (run.get("details") or {}).get("key")),
          (200, 403, 201, 429, "quota_exceeded", "no_ready_deployment", True, "quota_exceeded", "tokens.monthly"))

    # E28-11 이벤트
    subj = subjects()
    want = ["plt.iam.user_deprovisioned", "plt.quota.threshold", "plt.quota.exceeded"] + (["plt.iam.session_revoked"] if PROD else [])
    check("E28-11", sorted(s for s in want if s not in subj), [])
finally:
    cleanup()

print(f"== {MODE}: failed={failed}")
sys.exit(1 if failed else 0)
