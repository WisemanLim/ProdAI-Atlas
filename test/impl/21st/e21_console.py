#!/usr/bin/env python3
"""E21-12 콘솔 M3 화면 E2E — prod(oidc)면 시험용 플랫폼 관리자를 Keycloak 에 만들고(무작위 비밀번호) 돌린 뒤 지운다. local 은 그대로 실행.
사용: e18_console.py <console_base_url> <screenshot_dir> [keycloak_admin_password]"""
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request

BASE, SHOTS = sys.argv[1], sys.argv[2]
PW = sys.argv[3] if len(sys.argv) > 3 else ""
KC = "http://127.0.0.1:8180"
CONSOLE = os.path.join(os.path.dirname(__file__), "../../../apps/console")


def call(method, url, body=None, token=None, form=False):
    h, data = {}, None
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        h["content-type"] = "application/x-www-form-urlencoded" if form else "application/json"
    if token:
        h["authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, data, h, method=method), timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def run(env=None):
    return subprocess.run(["node", "e2e/m3.e2e.mjs", BASE, os.path.abspath(SHOTS)], cwd=CONSOLE, env={**os.environ, **(env or {})}).returncode


os.makedirs(SHOTS, exist_ok=True)
if not PW:
    sys.exit(run())
adm = call("POST", f"{KC}/realms/master/protocol/openid-connect/token", {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": PW}, form=True)["access_token"]
A = f"{KC}/admin/realms/atlas"
user, pw = f"e2e-{secrets.token_hex(3)}", secrets.token_urlsafe(18)
call("POST", f"{A}/users", {"username": user, "enabled": True, "email": f"{user}@example.test", "emailVerified": True, "firstName": "E2E", "lastName": "M3",
                            "attributes": {"atlas_org": ["default"]}, "credentials": [{"type": "password", "value": pw, "temporary": False}]}, adm)
uid = call("GET", f"{A}/users?username={user}&exact=true", token=adm)[0]["id"]
call("POST", f"{A}/users/{uid}/role-mappings/realm", [call("GET", f"{A}/roles/platform-admin", token=adm)], adm)
try:
    rc = run({"ATLAS_E2E_USER": user, "ATLAS_E2E_PASS": pw})
finally:
    urllib.request.urlopen(urllib.request.Request(f"{A}/users/{uid}", method="DELETE", headers={"authorization": f"Bearer {adm}"}), timeout=30)
sys.exit(rc)
