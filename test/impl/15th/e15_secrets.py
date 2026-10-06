#!/usr/bin/env python3
"""Epic 15 비밀 스택 스모크 — local(file 저장소)·prod(OpenBao) 공용.
사용: e15_secrets.py <env:local|prod> [gateway_admin_token] [gateway_vk_for_rotation]
인증 모드(oidc)면 ATLAS_TEST_TOKEN(플랫폼 관리 API 키)을 Manager 호출에 붙인다. 비밀 값은 출력하지 않는다(길이·해시만)."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENV = sys.argv[1]
GW_ADMIN = sys.argv[2] if len(sys.argv) > 2 else ""
GW_VK = sys.argv[3] if len(sys.argv) > 3 else ""
M, GR, G = "http://127.0.0.1:8080", "http://127.0.0.1:8005", "http://127.0.0.1:8090"
ADMIN = os.environ.get("ATLAS_TEST_TOKEN", "")
failed = 0


def http(method, url, body=None, token=None):
    h = {"content-type": "application/json"}
    if token:
        h["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
            return r.status, json.loads(b or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b or b"{}"), dict(e.headers)
        except ValueError:
            return e.code, {}, dict(e.headers)


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


def gr_secret_version():
    return (http("GET", f"{GR}/readyz")[1].get("registration", {}).get("secrets") or {}).get("guard-secret")


def agent(name):
    items = http("GET", f"{M}/api/v1/agents?org=default", token=ADMIN)[1].get("items", [])
    return next((a for a in items if a["name"] == name), None)


backend = "bao" if ENV == "prod" else "file"

# E15-01 저장소 추상화 (SEC-01): 백엔드 종류, 이관된 비밀 목록(값 없음)
st, lst, _ = http("GET", f"{M}/api/v1/orgs/default/secrets", token=ADMIN)
names = {i["name"]: i["version"] for i in lst.get("items", [])}
check("E15-01", (st, lst.get("backend"), "guard-secret" in names, "value" in json.dumps(lst)), (200, backend, True, False))

# E15-02 주입 드라이버 (SEC-03): gr-io 는 선언 비밀을 중개로 받는다, env 비밀 차단, 파일 0400
for _ in range(30):
    if gr_secret_version():
        break
    time.sleep(1)
v0 = gr_secret_version()
cname = f"prodai-atlas-{ENV}-agent-gr-1"
allow = subprocess.run(["docker", "exec", cname, "sh", "-c", "echo ${ATLAS_ALLOW_ENV_SECRETS:-unset}"], capture_output=True, text=True).stdout.strip()
fmode = subprocess.run(["docker", "exec", cname, "sh", "-c", "stat -c %a /run/secrets/atlas/guard-secret 2>/dev/null || stat -c %a /tmp/atlas-secrets/guard-secret"],
                       capture_output=True, text=True).stdout.strip()
chk = http("POST", f"{GR}/v1/guard/check", {"tenant": "default", "boundary": "input", "contents": ["주민번호 900101-1234568 확인"]})[1]
check("E15-02", (v0 is not None, allow, fmode, chk.get("decision")), (True, "false", "400", "transform"))

# E15-03 최소 권한 (SEC-08): 선언하지 않은 에이전트(al-plan)는 못 읽고, 관리자도 값은 못 본다
plan = agent("al-plan")
tok = http("POST", f"{M}/api/v1/agents/{plan['id']}/token", token=ADMIN)[1].get("token", "")
s_undecl, b_undecl, _ = http("GET", f"{M}/api/v1/secrets/guard-secret", token=tok)
s_admin, b_admin, _ = http("GET", f"{M}/api/v1/secrets/guard-secret", token=ADMIN or None)
gr = agent("gr-io")
gtok = http("POST", f"{M}/api/v1/agents/{gr['id']}/token", token=ADMIN)[1].get("token", "")
s_ok, b_ok, h_ok = http("GET", f"{M}/api/v1/secrets/guard-secret", token=gtok)
check("E15-03", (s_undecl, b_undecl.get("error"), s_admin, b_admin.get("error"), s_ok, len(b_ok.get("value", "")) > 0, h_ok.get("Cache-Control")),
      (403, "secret_not_declared", 403, "agent_only", 200, True, "no-store"))

# E15-04 회전 (SEC-04 비밀 일반): 새 버전 → 에이전트가 갱신 주기 안에 반영, 이전 버전 보존
new = "rotated-" + secrets.token_hex(12)
s_rot, b_rot, _ = http("POST", f"{M}/api/v1/orgs/default/secrets/guard-secret/rotate", {"value": new}, ADMIN)
for _ in range(40):
    if gr_secret_version() == b_rot.get("version"):
        break
    time.sleep(1)
s_after, b_after, _ = http("GET", f"{M}/api/v1/secrets/guard-secret", token=gtok)
check("E15-04", (s_rot, b_rot.get("previousVersion") == (b_rot.get("version") or 0) - 1, gr_secret_version() == b_rot.get("version"),
                 hashlib.sha256(b_after.get("value", "").encode()).hexdigest() == hashlib.sha256(new.encode()).hexdigest()), (200, True, True, True))

# E15-05 감사 연계 (SEC-08 → IAM-08): 성공·거부 read, 회전, 값 미포함
aud = http("GET", f"{M}/api/v1/orgs/default/audit?limit=200", token=ADMIN)[1].get("items", [])
reads = [(e["result"], json.loads(e["detail"]).get("agent")) for e in aud if e["action"] == "secret.read"]
check("E15-05", (("success", "gr-io") in reads, ("denied", "al-plan") in reads, any(e["action"] == "secret.rotate" for e in aud), new not in json.dumps(aud)),
      (True, True, True, True))

# E15-06 공급자 키 무중단 회전 (SEC-04 · GW-KEY) — Gateway 가 OpenAI 호환 업스트림을 쓸 때만(prod 리허설)
if GW_ADMIN and GW_VK:
    s_bad, b_bad, _ = http("POST", f"{G}/admin/upstream-keys/openai/rotate", {"key": "sk-invalid-" + secrets.token_hex(4)}, GW_ADMIN)
    before = http("GET", f"{G}/admin/upstream-keys/openai/rotation", token=GW_ADMIN)[1]
    s_good, b_good, _ = http("POST", f"{G}/admin/upstream-keys/openai/rotate", {"key": GW_VK}, GW_ADMIN)
    keys = http("GET", f"{G}/admin/upstream-keys", token=GW_ADMIN)[1].get("openai", [])
    check("E15-06", (s_bad, b_bad.get("error", {}).get("code"), s_good, b_good.get("version") == before.get("version", 0) + 1, b_good.get("retiringKeys"),
                     b_good.get("backend"), sorted(k["status"] for k in keys), GW_VK not in json.dumps(keys)),
          (422, "key_validation_failed", 200, True, 1, "bao", ["active", "retiring"], True))
else:
    print("SKIP E15-06 (Gateway OpenAI 호환 업스트림 미설정 — prod 리허설에서 실행)")
sys.exit(1 if failed else 0)
