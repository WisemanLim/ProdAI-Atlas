"""비밀 주입 드라이버 (FR-PLT-SEC-03·08) — Manager 중개를 흉내 낸 MockTransport 로 시험."""
import asyncio
import os
import stat

import httpx
import pytest

from atlas_agent_core import secrets as s


def manager(store, calls):
    def handle(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("authorization", "")))
        if req.url.path.endswith("/token"):
            if req.headers.get("authorization") != "Bearer atl_svc":
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"token": "agent-jwt", "expiresIn": 900})
        name = req.url.path.rsplit("/", 1)[-1]
        if req.headers.get("authorization") != "Bearer agent-jwt":
            return httpx.Response(403, json={"error": "agent_only"})
        if name not in store:
            return httpx.Response(403, json={"error": "secret_not_declared"})
        v, ver = store[name]
        return httpx.Response(200, json={"name": name, "value": v, "version": ver})
    return httpx.MockTransport(handle)


def test_fetch_caches_writes_file_and_blocks_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_GUARD_SECRET", "from-env")
    monkeypatch.delenv("ATLAS_ALLOW_ENV_SECRETS", raising=False)
    calls = []
    c = s.SecretClient("http://manager:8080", "atl_svc", ["guard-secret"], transport=manager({"guard-secret": ("v1", 1)}, calls))
    c.dir, c.agent_id = tmp_path, "a1"
    with pytest.raises(s.SecretUnavailable):  # 중개 전 env 는 차단(allowEnvSecrets=false)
        c.value("guard-secret", env="ATLAS_GUARD_SECRET")
    assert asyncio.run(c.fetch("guard-secret")) == "v1"
    assert c.value("guard-secret", env="ATLAS_GUARD_SECRET") == "v1"
    f = tmp_path / "guard-secret"
    assert f.read_text() == "v1" and stat.S_IMODE(os.stat(f).st_mode) == 0o400
    with pytest.raises(s.SecretUnavailable):
        asyncio.run(c.fetch("other"))
    assert [p for _, p, _ in calls].count("/api/v1/agents/a1/token") == 1  # 토큰 재사용


def test_rotation_picked_up_by_refresh(tmp_path):
    store, calls = {"guard-secret": ("v1", 1)}, []
    c = s.SecretClient("http://m", "atl_svc", ["guard-secret"], transport=manager(store, calls), refresh_s=0.05)
    c.dir = tmp_path
    state = {"agent_id": "a1"}

    async def go():
        job = asyncio.create_task(c.run(state))
        await asyncio.sleep(0.1)
        store["guard-secret"] = ("v2", 2)
        await asyncio.sleep(0.2)
        job.cancel()
    asyncio.run(go())
    assert c.value("guard-secret") == "v2" and state["secrets"] == {"guard-secret": 2}


def test_env_allowed_only_when_opted_in_or_standalone(monkeypatch):
    monkeypatch.setenv("X_SECRET", "env-v")
    assert s.SecretClient(None, None, []).value("x", env="X_SECRET") == "env-v"          # Manager 없이 단독 실행
    monkeypatch.setenv("ATLAS_ALLOW_ENV_SECRETS", "true")
    assert s.SecretClient("http://m", "atl_svc", []).value("x", env="X_SECRET") == "env-v"
    monkeypatch.setenv("X_SECRET", "CHANGE_ME")
    with pytest.raises(s.SecretUnavailable):
        s.SecretClient("http://m", "atl_svc", []).value("x", env="X_SECRET")


def test_stale_agent_token_is_refreshed_once(tmp_path):
    """Manager 재시작으로 캐시한 단기 토큰이 무효(401)면 새 토큰으로 1회 재시도."""
    calls = []
    inner = manager({"guard-secret": ("v1", 1)}, calls)

    def handle(req: httpx.Request):
        if req.headers.get("authorization") == "Bearer stale-jwt":
            calls.append((req.method, req.url.path, "stale"))
            return httpx.Response(401, json={"error": "invalid_token"})
        return inner.handle_request(req)
    c = s.SecretClient("http://m", "atl_svc", ["guard-secret"], transport=httpx.MockTransport(handle))
    c.dir, c.agent_id, c._token = tmp_path, "a1", ("stale-jwt", 9e12)
    assert asyncio.run(c.fetch("guard-secret")) == "v1"
    assert [x[2] for x in calls][:1] == ["stale"] and [p for _, p, _ in calls].count("/api/v1/agents/a1/token") == 1
