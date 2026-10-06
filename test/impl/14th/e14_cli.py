#!/usr/bin/env python3
"""E14-15 CLI SSO — atlas login(장치 코드) → 브라우저 승인(Playwright) → atlas whoami. 시험 계정은 무작위 비밀번호로 만들고 지운다.
사용: e14_cli.py <keycloak_admin_password>"""
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

PW = sys.argv[1]
KC = "http://127.0.0.1:8180"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))


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
user, pw = f"cli-{secrets.token_hex(3)}", secrets.token_urlsafe(18)
call("POST", f"{A}/users", {"username": user, "enabled": True, "email": f"{user}@example.test", "emailVerified": True, "firstName": "CLI", "lastName": "Test",
                            "attributes": {"atlas_org": ["default"]}, "credentials": [{"type": "password", "value": pw, "temporary": False}]}, adm)
uid = call("GET", f"{A}/users?username={user}&exact=true", token=adm)[0]["id"]
tmp = tempfile.mkdtemp()
binp = os.path.join(tmp, "atlas")
env = {**os.environ, "ATLAS_CONFIG_DIR": tmp, "ATLAS_E2E_USER": user, "ATLAS_E2E_PASS": pw}
failed = 0
try:
    subprocess.run(["go", "build", "-o", binp, "./cmd/atlas"], cwd=os.path.join(ROOT, "apps/manager"), check=True)
    p = subprocess.Popen([binp, "login", "--manager", "http://127.0.0.1:8080"], stdout=subprocess.PIPE, text=True, env=env)
    first = p.stdout.readline()
    url = re.search(r"(http\S+)", first).group(1)
    subprocess.run(["node", "e2e/device.e2e.mjs", url], cwd=os.path.join(ROOT, "apps/console"), env=env, check=True)
    rest = p.communicate(timeout=60)[0]
    mode = oct(os.stat(os.path.join(tmp, "credentials.json")).st_mode & 0o777)
    who = json.loads(subprocess.run([binp, "whoami", "--manager", "http://127.0.0.1:8080"], capture_output=True, text=True, env=env).stdout)
    got = (p.returncode, "로그인 완료" in rest, mode, who["principal"]["name"], who["principal"]["kind"], who["org"])
    want = (0, True, "0o600", user, "user", "default")
    failed = got != want
    print(f"{'FAIL' if failed else 'PASS'} E14-15 CLI 장치 코드 로그인 → whoami got={got}" + (f" want={want}" if failed else ""))
finally:
    urllib.request.urlopen(urllib.request.Request(f"{A}/users/{uid}", method="DELETE", headers={"authorization": f"Bearer {adm}"}), timeout=30)
sys.exit(1 if failed else 0)
