"""MCP-HOST OAuth 2.1 서비스 계정 — 401 탐지 → RFC 9728 PRM → OIDC Discovery → client_credentials(resource) · issuer 불일치 거부 · 캐시."""
import asyncio

import httpx
import pytest

from units.mcp_host import oauth


def world(issuer_meta="http://kc/realms/atlas"):
    calls = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url)))
        u = str(req.url)
        if req.method == "POST" and u == "http://hr:8000/mcp":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer resource_metadata="http://hr:8000/.well-known/oauth-protected-resource/mcp"'})
        if u == "http://hr:8000/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": "http://hr:8000/mcp", "authorization_servers": ["http://kc/realms/atlas"], "scopes_supported": ["hr:read"]})
        if u == "http://kc/realms/atlas/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": issuer_meta, "token_endpoint": "http://kc/realms/atlas/protocol/openid-connect/token"})
        if u.endswith("/token"):
            body = req.content.decode()
            assert "grant_type=client_credentials" in body and "resource=http%3A%2F%2Fhr%3A8000%2Fmcp" in body and "scope=hr%3Aread" in body
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 300})
        return httpx.Response(404)
    return httpx.MockTransport(handle), calls


def test_discovery_client_credentials_and_cache(monkeypatch):
    monkeypatch.setenv("ATLAS_MCP_OAUTH_CLIENT_SECRET", "s3cret")
    t, calls = world()
    oauth._CACHE.clear()
    www = asyncio.run(oauth.probe("http://hr:8000/mcp", transport=t))
    assert "resource_metadata" in www
    a = asyncio.run(oauth.token("http://hr:8000/mcp", "default", www, transport=t))
    b = asyncio.run(oauth.token("http://hr:8000/mcp", "default", www, transport=t))
    assert (a["access_token"], a["cached"], b["cached"], a["resource"]) == ("tok-1", False, True, "http://hr:8000/mcp")
    assert sum(1 for m, u in calls if u.endswith("/token")) == 1


def test_issuer_mixup_rejected(monkeypatch):
    monkeypatch.setenv("ATLAS_MCP_OAUTH_CLIENT_SECRET", "s3cret")
    t, _ = world(issuer_meta="http://evil/realms/atlas")
    oauth._CACHE.clear()
    with pytest.raises(oauth.OAuthError, match="issuer"):
        asyncio.run(oauth.token("http://hr:8000/mcp", "default", "", transport=t))


def test_internal_issuer_rewrite(monkeypatch):
    monkeypatch.setenv("ATLAS_OIDC_ISSUER", "http://127.0.0.1:8180/realms/atlas")
    monkeypatch.setenv("ATLAS_OIDC_ISSUER_INTERNAL", "http://keycloak:8080/realms/atlas")
    assert oauth.internal("http://127.0.0.1:8180/realms/atlas/protocol/openid-connect/token") == "http://keycloak:8080/realms/atlas/protocol/openid-connect/token"
    assert oauth.canonical("HTTP://HR:8000/mcp/") == "http://hr:8000/mcp"
