"""OAuth 2.1 보호 MCP 서버 (시험·개발 전용) — Keycloak 토큰만 받는다 (FR-MCP-HOST-03 시험 대상).

RFC 9728 보호 자원 메타데이터(/.well-known/oauth-protected-resource/mcp)·401 WWW-Authenticate 는 FastMCP RemoteAuthProvider 가 낸다.
issuer = 브라우저가 보는 Keycloak 주소(ATLAS_OIDC_ISSUER), JWKS 는 컨테이너 내부 주소로 받는다.
"""
from __future__ import annotations

import os

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

ISSUER = os.environ.get("ATLAS_OIDC_ISSUER", "http://127.0.0.1:8180/realms/atlas")
JWKS = os.environ.get("ATLAS_OIDC_JWKS_URL", "http://keycloak:8080/realms/atlas/protocol/openid-connect/certs")
PUBLIC = os.environ.get("ATLAS_PUBLIC_URL", "http://mcp-protected:8000")

server = FastMCP("protected-hr", instructions="사내 인사 데이터 (OAuth 보호)",
                 auth=RemoteAuthProvider(token_verifier=JWTVerifier(jwks_uri=JWKS, issuer=ISSUER), authorization_servers=[ISSUER], base_url=PUBLIC,
                                         resource_name="protected-hr"))


@server.tool(annotations={"readOnlyHint": True})
def payroll_summary(team: str = "platform") -> dict:
    """팀별 인건비 요약 (보호 자원)."""
    return {"team": team, "headcount": 12, "currency": "KRW"}


@server.tool(annotations={"readOnlyHint": True})
def whoami() -> dict:
    """호출 토큰의 주체 (서비스 계정이면 azp = client_id)."""
    t = get_access_token()
    return {"client_id": t.client_id if t else None, "scopes": t.scopes if t else []}


if __name__ == "__main__":
    uvicorn.run(server.http_app(path="/mcp"), host="0.0.0.0", port=8000, log_level="warning")
