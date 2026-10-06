"""A2A 테넌트 JWT 검증 (FR-PLT-TEN-06) — Manager 와 같은 ES256 토큰을 만들어 SDK 미들웨어를 시험한다."""
import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from atlas_agent_core import tenancy
from atlas_agent_core.a2a import A2AHandler


class FakeJWKS:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.key.public_key()})()


def make_auth(mode="verify"):
    key = ec.generate_private_key(ec.SECP256R1())
    a = tenancy.TenantAuth(mode, None)
    a._client = FakeJWKS(key)
    return a, key


def mint(key, org, iss="atlas-manager", exp=60, aud="agent:abc"):
    now = int(time.time())
    return jwt.encode({"iss": iss, "sub": "u1", "aud": [aud], "atlas_org": org, "iat": now, "exp": now + exp}, key, algorithm="ES256")


def run(c):
    return asyncio.run(c)


def test_verify_mode_allows_missing_token_and_checks_org():
    a, key = make_auth()
    assert run(a.check({}, "acme")) is None
    claims = run(a.check({"authorization": "Bearer " + mint(key, "acme")}, "acme"))
    assert claims["atlas_org"] == "acme"
    with pytest.raises(PermissionError) as e:
        run(a.check({"authorization": "Bearer " + mint(key, "beta")}, "acme"))
    assert e.value.args[0] == tenancy.TENANT_MISMATCH


def test_required_mode_and_bad_tokens():
    a, key = make_auth("required")
    with pytest.raises(PermissionError) as e:
        run(a.check({}, "acme"))
    assert e.value.args[0] == tenancy.UNAUTHENTICATED
    other = ec.generate_private_key(ec.SECP256R1())
    for bad in (mint(other, "acme"), mint(key, "acme", iss="evil"), mint(key, "acme", exp=-10), mint(key, "acme", aud="user:x")):
        with pytest.raises(PermissionError):
            run(a.check({"authorization": "Bearer " + bad}, "acme"))


def test_handler_rejects_mismatch_as_jsonrpc_error(client):
    a, key = make_auth()
    h = A2AHandler(client.app.state.spec, client.app.state.store, a)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {"tenant": "acme"}}
    out = run(h.dispatch(body, {"authorization": "Bearer " + mint(key, "beta")}))
    assert out["error"]["code"] == tenancy.TENANT_MISMATCH
    ok = run(h.dispatch(body, {"authorization": "Bearer " + mint(key, "acme")}))
    assert "result" in ok, json.dumps(ok)
