#!/usr/bin/env python3
"""Epic 14 기업·IAM·격리 스택 시험 — ATLAS_AUTH_MODE=oidc 인 스택 대상 (local·prod 공용).
사용: e14_iam.py <env:local|prod> <keycloak_admin_password> <ci_secret>
Keycloak 관리 API 로 시험 사용자·임시 직접 로그인 클라이언트(atlas-e2e)를 만들고 끝나면 지운다(비밀번호는 실행마다 무작위).
마지막 줄에 회귀 게이트용 플랫폼 관리 API 키를 E14-GATE-KEY 로 출력하지 않는다 — 별도 --gate-key 모드에서만."""
import io
import json
import secrets
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ENV, KC_ADMIN_PW, CI_SECRET = sys.argv[1], sys.argv[2], sys.argv[3]
M, KC = "http://127.0.0.1:8080", "http://127.0.0.1:8180"
RUN = secrets.token_hex(3)
failed = 0


def http(method, url, body=None, token=None, form=False, headers=None, raw=False):
    h = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data, h["content-type"] = urllib.parse.urlencode(body).encode(), "application/x-www-form-urlencoded"
        else:
            data, h["content-type"] = json.dumps(body).encode(), "application/json"
    if token:
        h["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
            return r.status, (b if raw else _j(b)), dict(r.headers)
    except urllib.error.HTTPError as e:
        b = e.read()
        return e.code, (b if raw else _j(b)), dict(e.headers)


def _j(b):
    try:
        return json.loads(b or b"{}")
    except ValueError:
        return {"_raw": b[:200].decode(errors="replace")}


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


# ---- Keycloak 관리: 시험 사용자·임시 클라이언트 ----
adm = http("POST", f"{KC}/realms/master/protocol/openid-connect/token",
           {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": KC_ADMIN_PW}, form=True)[1]["access_token"]
A = f"{KC}/admin/realms/atlas"
E2E_SECRET = secrets.token_urlsafe(24)
http("POST", f"{A}/clients", {"clientId": f"atlas-e2e-{RUN}", "publicClient": False, "secret": E2E_SECRET, "directAccessGrantsEnabled": True,
                               "standardFlowEnabled": False, "protocolMappers": [{"name": "atlas_org", "protocol": "openid-connect",
                               "protocolMapper": "oidc-usermodel-attribute-mapper", "config": {"user.attribute": "atlas_org", "claim.name": "atlas_org",
                               "jsonType.label": "String", "access.token.claim": "true"}}]}, adm)
users = {}


def kc_user(name, org, platform=False):
    pw = secrets.token_urlsafe(18)
    uname = f"{name}-{RUN}"
    http("POST", f"{A}/users", {"username": uname, "enabled": True, "email": f"{uname}@example.test", "emailVerified": True, "firstName": name, "lastName": "test",
                                "attributes": {"atlas_org": [org]}, "credentials": [{"type": "password", "value": pw, "temporary": False}]}, adm)
    uid = http("GET", f"{A}/users?username={uname}&exact=true", token=adm)[1][0]["id"]
    if platform:
        role = http("GET", f"{A}/roles/platform-admin", token=adm)[1]
        http("POST", f"{A}/users/{uid}/role-mappings/realm", [role], adm)
    users[name] = {"id": uid, "username": uname, "password": pw}
    return uid


def token(name):
    u = users[name]
    return http("POST", f"{KC}/realms/atlas/protocol/openid-connect/token", {"grant_type": "password", "client_id": f"atlas-e2e-{RUN}",
                "client_secret": E2E_SECRET, "username": u["username"], "password": u["password"], "scope": "openid"}, form=True)[1]["access_token"]


ACME, BETA = f"acme-{RUN}", f"beta-{RUN}"
kc_user("padmin", "default", platform=True)
kc_user("alice", ACME)
kc_user("bob", ACME)
kc_user("carol", BETA)
kc_user("dave", ACME)
T = {n: token(n) for n in users}
MANIFEST = {"apiVersion": "agents.prodai.io/v1alpha1", "kind": "AtlasAgent", "metadata": {"name": "iam-probe", "unitId": "AL-PLAN"},
            "spec": {"version": "0.1.0", "interfaces": [{"binding": "JSONRPC", "port": 8000, "path": "/a2a"}]}}


def manifest(name):
    m = json.loads(json.dumps(MANIFEST))
    m["metadata"]["name"] = name
    return m


try:
    # E14-01 OIDC 검증 경로 · 공개 엔드포인트 · 401
    cfg = http("GET", f"{M}/api/v1/auth/config")[1]
    me = http("GET", f"{M}/api/v1/auth/me", token=T["alice"])[1]
    check("E14-01", (cfg.get("mode"), cfg.get("clientId"), http("GET", f"{M}/api/v1/agents")[0], http("GET", f"{M}/api/v1/agents", token="eyJ.bad.tok")[0],
                     http("GET", f"{M}/api/v1/catalog/units")[0], http("GET", f"{M}/.well-known/jwks.json")[0], me.get("org"), me.get("principal", {}).get("kind")),
          ("oidc", "atlas-console", 401, 401, 200, 200, ACME, "user"))

    # E14-02 조직 수명: 플랫폼 관리자만 생성, 소유자 지정, 기본 프로젝트·환경 (TEN-01·02)
    s_na = http("POST", f"{M}/api/v1/orgs", {"slug": ACME}, T["alice"])[0]
    s1, o1, _ = http("POST", f"{M}/api/v1/orgs", {"slug": ACME, "name": "Acme", "dataRegion": "kr", "owner": users["alice"]["id"]}, T["padmin"])
    s2 = http("POST", f"{M}/api/v1/orgs", {"slug": BETA, "dataRegion": "eu", "owner": users["carol"]["id"]}, T["padmin"])[0]
    envs = [e["name"] for e in http("GET", f"{M}/api/v1/orgs/{ACME}/projects/default/envs", token=T["alice"])[1].get("items", [])]
    check("E14-02", (s_na, s1, o1.get("status"), o1.get("isolation"), s2, envs), (403, 201, "active", "shared", 201, ["dev", "staging", "prod"]))

    # E14-03 RBAC 역할 템플릿 (IAM-04): 소유자 → 프로젝트·역할 부여, 개발자 생성 O·배포 X → deployer 부여 후 배포 O, viewer 읽기 전용
    H = {"X-Atlas-Org": ACME}
    sp = http("POST", f"{M}/api/v1/orgs/{ACME}/projects", {"slug": "web"}, T["alice"])[0]
    http("POST", f"{M}/api/v1/orgs/{ACME}/members", {"user": users["bob"]["id"], "role": "agent-developer", "project": "web"}, T["alice"])
    http("POST", f"{M}/api/v1/orgs/{ACME}/members", {"user": users["dave"]["id"], "role": "viewer"}, T["alice"])
    sb, ag, _ = http("POST", f"{M}/api/v1/agents?project=web", manifest("probe-a"), T["bob"], headers=H)
    aid = ag.get("id", "")
    dep = {"agentId": aid, "env": "prod", "driver": "compose", "endpoint": "http://agent-al-plan:8000"}
    sd1 = http("POST", f"{M}/api/v1/deployments", dep, T["bob"], headers=H)[0]
    http("POST", f"{M}/api/v1/orgs/{ACME}/members", {"user": users["bob"]["id"], "role": "env-deployer", "project": "web", "env": "staging"}, T["alice"])
    sd2 = http("POST", f"{M}/api/v1/deployments", {**dep, "env": "staging"}, T["bob"], headers=H)[0]
    sv_read = http("GET", f"{M}/api/v1/agents/{aid}", token=T["dave"], headers=H)[0]
    sv_write = http("POST", f"{M}/api/v1/agents?project=web", manifest("probe-v"), T["dave"], headers=H)[0]
    members = http("GET", f"{M}/api/v1/orgs/{ACME}/members", token=T["alice"])[1].get("items", [])
    check("E14-03", (sp, sb, sd1, sd2, sv_read, sv_write, len(members) >= 3), (201, 201, 403, 201, 200, 403, True))  # owner·viewer·developer

    # E14-04 크로스테넌트 누출 차단 (CMP-01): beta 사용자는 acme 자원을 못 본다
    c_get = http("GET", f"{M}/api/v1/agents/{aid}", token=T["carol"])[0]
    c_hdr = http("GET", f"{M}/api/v1/agents/{aid}", token=T["carol"], headers=H)[0]
    c_list = len(http("GET", f"{M}/api/v1/agents", token=T["carol"])[1].get("items", []))
    c_org = http("GET", f"{M}/api/v1/orgs/{ACME}", token=T["carol"])[0]
    c_aud = http("GET", f"{M}/api/v1/orgs/{ACME}/audit", token=T["carol"])[0]
    orgs_bob = sorted(o["slug"] for o in http("GET", f"{M}/api/v1/orgs", token=T["bob"])[1].get("items", []))
    check("E14-04", (c_get, c_hdr, c_list, c_org, c_aud, orgs_bob), (404, 403, 0, 403, 403, []))  # bob 은 프로젝트 역할만 → 조직 viewer 아님

    # E14-05 쿼터 (TEN-03): 조직 agents.max=2 → 3번째 429, 프로젝트 합 > 조직 거부
    http("PUT", f"{M}/api/v1/orgs/{ACME}/quotas", {"scope": "org", "key": "agents.max", "limit": 2}, T["padmin"])
    q2 = http("POST", f"{M}/api/v1/agents?project=web", manifest("probe-b"), T["bob"], headers=H)[0]
    q3, qb, _ = http("POST", f"{M}/api/v1/agents?project=web", manifest("probe-c"), T["bob"], headers=H)
    qp = http("PUT", f"{M}/api/v1/orgs/{ACME}/quotas", {"scope": "project", "project": "web", "key": "agents.max", "limit": 3}, T["alice"])[0]
    check("E14-05", (q2, q3, qb.get("error"), qp), (201, 429, "quota_exceeded", 422))

    # E14-06 서비스 계정 (IAM-06 client credentials): 역할 부여 전 403 → deployer 부여 후 배포
    ci = http("POST", f"{KC}/realms/atlas/protocol/openid-connect/token", {"grant_type": "client_credentials", "client_id": "atlas-ci", "client_secret": CI_SECRET}, form=True)[1]["access_token"]
    ci_me = http("GET", f"{M}/api/v1/auth/me", token=ci)[1]["principal"]
    ci_dep = {**dep, "env": "dev"}
    s_ci1 = http("POST", f"{M}/api/v1/deployments", ci_dep, ci, headers=H)[0]
    http("POST", f"{M}/api/v1/orgs/{ACME}/members", {"user": ci_me["sub"], "role": "deployer", "project": "web"}, T["alice"])
    s_ci2 = http("POST", f"{M}/api/v1/deployments", ci_dep, ci, headers=H)[0]
    check("E14-06", (ci_me["kind"], s_ci1, s_ci2), ("service", 403, 201))

    # E14-07 API 키 (IAM-06): 범위·만료 필수, read 키는 쓰기 불가, 폐기 즉시 401
    sk, kb, _ = http("POST", f"{M}/api/v1/orgs/{ACME}/apikeys", {"name": "ci-read", "scopes": "read", "expiresInDays": 7}, T["alice"])
    key = kb.get("key", "")
    k_read = http("GET", f"{M}/api/v1/agents?project=web", token=key)[0]
    k_write = http("POST", f"{M}/api/v1/agents?project=web", manifest("probe-k"), key)[0]
    k_nexp = http("POST", f"{M}/api/v1/orgs/{ACME}/apikeys", {"name": "x", "scopes": "read"}, T["alice"])[0]
    http("DELETE", f"{M}/api/v1/orgs/{ACME}/apikeys/{kb.get('apiKey', {}).get('id')}", token=T["alice"])
    k_rev = http("GET", f"{M}/api/v1/agents", token=key)[0]
    check("E14-07", (sk, key.startswith(f"atl_{ENV}_"), k_read, k_write, k_nexp, k_rev), (201, True, 200, 403, 400, 401))

    # E14-08 A2A 테넌트 전파 (TEN-06): 프록시가 tenant 강제·JWT 서명, 불일치 거부, 에이전트는 위조 토큰 거부
    plan = next(a for a in http("GET", f"{M}/api/v1/agents?org=default", token=T["padmin"])[1]["items"] if a["name"] == "al-plan")
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "테넌트 확인. 그리고 끝"}]}}}
    st_ok, r_ok, _ = http("POST", f"{M}/api/v1/agents/{plan['id']}/a2a?org=default", rpc, T["padmin"])
    st_mm, r_mm, _ = http("POST", f"{M}/api/v1/agents/{plan['id']}/a2a?org=default", {**rpc, "params": {**rpc["params"], "tenant": ACME}}, T["padmin"])
    forged = "eyJhbGciOiJFUzI1NiIsImtpZCI6Inh4In0.eyJpc3MiOiJhdGxhcy1tYW5hZ2VyIiwiYXRsYXNfb3JnIjoiZGVmYXVsdCJ9.AAAA"
    st_f, r_f, _ = http("POST", "http://127.0.0.1:8000/a2a", rpc, forged)
    check("E14-08", (st_ok, r_ok.get("result", {}).get("task", {}).get("status", {}).get("state"), st_mm, r_mm.get("error"), r_f.get("error", {}).get("code")),
          (200, "TASK_STATE_COMPLETED", 403, "tenant_mismatch", -32051))

    # E14-09 조직 정지 (TEN-01): 정지 중 사용자 API 403·에이전트 호출 401, 재개 후 정상
    http("PATCH", f"{M}/api/v1/orgs/{BETA}", {"status": "suspended"}, T["padmin"])
    s_sus = http("POST", f"{M}/api/v1/agents", manifest("beta-x"), T["carol"])
    s_carol_proj = http("POST", f"{M}/api/v1/orgs/{BETA}/projects", {"slug": "p2"}, T["carol"])[0]
    http("PATCH", f"{M}/api/v1/orgs/{BETA}", {"status": "active"}, T["padmin"])
    s_res = http("POST", f"{M}/api/v1/orgs/{BETA}/projects", {"slug": "p2"}, T["carol"])[0]
    check("E14-09", (s_sus[0], s_sus[1].get("error"), s_carol_proj, s_res), (403, "org_suspended", 403, 201))

    # E14-10 감사 로그 (IAM-08): 체인 유효·주요 행위 기록·머클 루트·변경 불가
    aud = http("GET", f"{M}/api/v1/orgs/{ACME}/audit?limit=200", token=T["alice"])[1].get("items", [])
    actions = {e["action"] for e in aud}
    ver = http("GET", f"{M}/api/v1/orgs/{ACME}/audit/verify", token=T["alice"])[1]
    root = http("GET", f"{M}/api/v1/orgs/{ACME}/audit/root", token=T["alice"])[1]
    need = {"org.create", "project.create", "member.add", "agent.create", "deploy.create", "authz.denied", "apikey.create", "apikey.revoke", "quota.set"}
    check("E14-10", (need <= actions, ver.get("valid"), ver.get("events", 0) >= 10, len(root.get("merkleRoot", "")), all(e["actorId"] for e in aud)),
          (True, True, True, 64, True))

    # E14-11 내보내기 (TEN-01): tar.gz — 매니페스트·감사로그 포함, 비밀 제외
    se, blob, hdr = http("GET", f"{M}/api/v1/orgs/{ACME}/export", token=T["alice"], raw=True)
    names = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").getnames() if se == 200 else []
    check("E14-11", (se, "organization.json" in names, "audit.jsonl" in names, any(n.startswith("agents/web/") for n in names),
                     b'"salt"' not in blob), (200, True, True, True, True))  # API 키 해시·salt 미포함

    # E14-12 30일 유예 삭제
    sdel, odel, _ = http("DELETE", f"{M}/api/v1/orgs/{BETA}", token=T["padmin"])
    days = (time.mktime(time.strptime(odel.get("deleteAfter", "1970-01-01T00:00:00")[:19], "%Y-%m-%dT%H:%M:%S")) - time.time()) / 86400
    check("E14-12", (sdel, odel.get("status"), 29 <= days <= 30.1, http("DELETE", f"{M}/api/v1/orgs/default", token=T["padmin"])[0]), (202, "deleting", True, 409))

    # E14-13 콘솔 프록시 /svc: tenant 강제·관리 경로 차단
    s_svc_ok = http("GET", f"{M}/svc/gr/v1/guard/verdicts?tenant={ACME}&limit=1", token=T["alice"], headers=H)[0]
    s_svc_mm = http("GET", f"{M}/svc/gr/v1/guard/verdicts?tenant=default&limit=1", token=T["alice"], headers=H)[0]
    s_svc_adm = http("GET", f"{M}/svc/gw/admin/keys", token=T["alice"], headers=H)[0]
    check("E14-13", (s_svc_ok, s_svc_mm, s_svc_adm, http("GET", f"{M}/svc/gr/v1/guard/verdicts", token=None)[0]), (200, 403, 403, 401))
finally:
    for u in users.values():
        http("DELETE", f"{A}/users/{u['id']}", token=adm)
    cid = (http("GET", f"{A}/clients?clientId=atlas-e2e-{RUN}", token=adm)[1] or [{}])[0].get("id")
    if cid:
        http("DELETE", f"{A}/clients/{cid}", token=adm)
    print(f"cleanup: users={len(users)} client=atlas-e2e-{RUN}")
sys.exit(1 if failed else 0)
