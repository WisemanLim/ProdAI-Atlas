#!/usr/bin/env python3
"""E14-14 콘솔 SSO E2E — 시험용 플랫폼 관리자 계정을 Keycloak 에 만들고(무작위 비밀번호) 콘솔 UI E2E 를 로그인부터 돌린 뒤 지운다.
사용: e14_console.py <keycloak_admin_password> <console_base_url> <screenshot_dir>"""
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request

PW, BASE, SHOTS = sys.argv[1], sys.argv[2], sys.argv[3]
KC = "http://127.0.0.1:8180"


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


adm = call("POST", f"{KC}/realms/master/protocol/openid-connect/token", {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": PW}, form=True)["access_token"]
A = f"{KC}/admin/realms/atlas"
user, pw = f"e2e-{secrets.token_hex(3)}", secrets.token_urlsafe(18)
call("POST", f"{A}/users", {"username": user, "enabled": True, "email": f"{user}@example.test", "emailVerified": True, "firstName": "E2E", "lastName": "Console",
                            "attributes": {"atlas_org": ["default"]}, "credentials": [{"type": "password", "value": pw, "temporary": False}]}, adm)
uid = call("GET", f"{A}/users?username={user}&exact=true", token=adm)[0]["id"]
call("POST", f"{A}/users/{uid}/role-mappings/realm", [call("GET", f"{A}/roles/platform-admin", token=adm)], adm)
try:
    rc = subprocess.run(["node", "e2e/console.e2e.mjs", BASE, os.path.abspath(SHOTS)], cwd=os.path.join(os.path.dirname(__file__), "../../../apps/console"),
                        env={**os.environ, "ATLAS_E2E_USER": user, "ATLAS_E2E_PASS": pw}).returncode
finally:
    urllib.request.urlopen(urllib.request.Request(f"{A}/users/{uid}", method="DELETE", headers={"authorization": f"Bearer {adm}"}), timeout=30)
sys.exit(rc)
