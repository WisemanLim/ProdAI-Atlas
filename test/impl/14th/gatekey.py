#!/usr/bin/env python3
"""회귀 게이트용 플랫폼 관리 API 키 발급 — 임시 플랫폼 관리자 계정·직접 로그인 클라이언트로 토큰을 받아 default 조직 admin 범위 키(1일)를 만들고
계정·클라이언트는 지운다. 표준출력 마지막 줄 = 평문 키(파일에 저장하지 말 것). 사용: gatekey.py <keycloak_admin_password>"""
import json
import secrets
import os
import sys
import urllib.parse
import urllib.request

PW, KC, M = sys.argv[1], "http://127.0.0.1:8180", os.environ.get("ATLAS_MANAGER_HOST_URL", "http://127.0.0.1:8080")


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
A, run = f"{KC}/admin/realms/atlas", secrets.token_hex(3)
user, pw, csec = f"gate-{run}", secrets.token_urlsafe(18), secrets.token_urlsafe(24)
call("POST", f"{A}/clients", {"clientId": f"atlas-gate-{run}", "publicClient": False, "secret": csec, "directAccessGrantsEnabled": True, "standardFlowEnabled": False}, adm)
call("POST", f"{A}/users", {"username": user, "enabled": True, "email": f"{user}@example.test", "emailVerified": True, "firstName": "Gate", "lastName": "Key",
                            "credentials": [{"type": "password", "value": pw, "temporary": False}]}, adm)
uid = call("GET", f"{A}/users?username={user}&exact=true", token=adm)[0]["id"]
call("POST", f"{A}/users/{uid}/role-mappings/realm", [call("GET", f"{A}/roles/platform-admin", token=adm)], adm)
try:
    tok = call("POST", f"{KC}/realms/atlas/protocol/openid-connect/token", {"grant_type": "password", "client_id": f"atlas-gate-{run}", "client_secret": csec,
               "username": user, "password": pw}, form=True)["access_token"]
    key = call("POST", f"{M}/api/v1/orgs/default/apikeys", {"name": f"regression-gate-{run}", "scopes": "admin", "expiresInDays": 1}, tok)["key"]
finally:
    urllib.request.urlopen(urllib.request.Request(f"{A}/users/{uid}", method="DELETE", headers={"authorization": f"Bearer {adm}"}), timeout=30)
    cid = call("GET", f"{A}/clients?clientId=atlas-gate-{run}", token=adm)[0]["id"]
    urllib.request.urlopen(urllib.request.Request(f"{A}/clients/{cid}", method="DELETE", headers={"authorization": f"Bearer {adm}"}), timeout=30)
print(key)
