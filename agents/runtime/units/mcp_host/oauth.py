"""MCP-HOST OAuth 2.1 클라이언트 (FR-MCP-HOST-03) — 서비스 계정 흐름.

401 + WWW-Authenticate(resource_metadata) → RFC 9728 보호 자원 메타데이터 → authorization_servers[0] →
OIDC Discovery(/.well-known/openid-configuration, 없으면 RFC 8414) → client_credentials(+ resource = 서버 canonical URI, RFC 8707) →
토큰을 (tenant, authServer, resource) 키로 캐시(만료 60초 전 갱신). 발급 메타데이터 issuer 가 보호 자원이 밝힌 AS 와 다르면 거부(mix-up 방지, RFC 9207 취지).
클라이언트 비밀은 Manager 비밀 중개(`mcp-oauth-client-secret`)로만 받는다. 사용자 위임(Authorization Code + PKCE)은 콘솔 흐름 — 후속.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from atlas_agent_core import secrets

_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}


class OAuthError(RuntimeError):
    pass


def canonical(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc.lower()}{u.path.rstrip('/') or ''}"


def internal(url: str) -> str:
    """브라우저용 issuer 주소 → 컨테이너가 닿는 내부 주소 (ATLAS_OIDC_ISSUER_INTERNAL 접두 치환)."""
    pub, inner = os.environ.get("ATLAS_OIDC_ISSUER", ""), os.environ.get("ATLAS_OIDC_ISSUER_INTERNAL", "")
    return inner + url[len(pub):] if pub and inner and url.startswith(pub) else url


def resource_metadata_url(url: str, www_authenticate: str) -> str:
    m = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "")
    if m:
        return m.group(1)
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource{u.path}"


async def probe(url: str, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """인증 없이 initialize 를 보내 401 이면 WWW-Authenticate 를 돌려준다(보호 서버가 아니면 None)."""
    body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "atlas-mcp-host", "version": "0.1"}}}
    async with httpx.AsyncClient(timeout=10, transport=transport) as c:
        r = await c.post(url, json=body, headers={"accept": "application/json, text/event-stream"})
    return r.headers.get("www-authenticate", "Bearer") if r.status_code == 401 else None


async def token(url: str, tenant: str, www_authenticate: str = "", transport: httpx.AsyncBaseTransport | None = None,
                fresh: bool = False) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15, transport=transport) as c:
        prm_url = resource_metadata_url(url, www_authenticate)
        r = await c.get(internal(prm_url))
        if r.status_code != 200:
            raise OAuthError(f"보호 자원 메타데이터 조회 실패 {r.status_code} ({prm_url})")
        prm = r.json()
        servers = prm.get("authorization_servers") or []
        if not servers:
            raise OAuthError("authorization_servers 없음")
        issuer = str(servers[0]).rstrip("/")
        resource = canonical(prm.get("resource") or url)
        key = (tenant, issuer, resource)
        hit = _CACHE.get(key)
        if hit and not fresh and hit[1] - 60 > time.time():
            return {"access_token": hit[0], "issuer": issuer, "resource": resource, "cached": True}
        meta = None
        for wk in ("/.well-known/openid-configuration", "/.well-known/oauth-authorization-server"):
            m = await c.get(internal(issuer + wk))
            if m.status_code == 200:
                meta = m.json()
                break
        if meta is None:
            raise OAuthError(f"인가 서버 메타데이터 없음 ({issuer})")
        if str(meta.get("issuer", "")).rstrip("/") != issuer:
            raise OAuthError(f"issuer 불일치 {meta.get('issuer')} ≠ {issuer} (mix-up 의심)")
        client_id = os.environ.get("ATLAS_MCP_OAUTH_CLIENT_ID", "atlas-ci")
        try:
            secret = await secrets.get("mcp-oauth-client-secret", env="ATLAS_MCP_OAUTH_CLIENT_SECRET")
        except secrets.SecretUnavailable as exc:
            raise OAuthError("클라이언트 비밀 없음 (조직 비밀 mcp-oauth-client-secret)") from exc
        scopes = " ".join(prm.get("scopes_supported") or [])
        form = {"grant_type": "client_credentials", "resource": resource, **({"scope": scopes} if scopes else {})}
        t = await c.post(internal(meta["token_endpoint"]), data=form, auth=(client_id, secret))
        if t.status_code != 200:
            raise OAuthError(f"토큰 발급 실패 {t.status_code}: {t.text[:200]}")
        b = t.json()
    _CACHE[key] = (b["access_token"], time.time() + float(b.get("expires_in", 300)))
    return {"access_token": b["access_token"], "issuer": issuer, "resource": resource, "cached": False}
