"""A2A 테넌트 전파 검증 (FR-PLT-TEN-06): Manager 가 ES256 으로 서명한 단기 JWT 의 atlas_org 클레임과 요청 tenant 를 비교한다.

ATLAS_A2A_AUTH: off(검사 안 함) · verify(토큰이 있으면 검증 — 기본) · required(토큰 필수)
ATLAS_A2A_JWKS_URL: 기본 {ATLAS_MANAGER_URL}/.well-known/jwks.json
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import jwt
from opentelemetry import trace

log = logging.getLogger("atlas.tenancy")

UNAUTHENTICATED, TENANT_MISMATCH = -32051, -32050  # JSON-RPC 서버 오류 범위 (-32000 ~ -32099)


class TenantAuth:
    def __init__(self, mode: str, jwks_url: str | None, issuer: str = "atlas-manager"):
        self.mode, self.jwks_url, self.issuer = mode, jwks_url, issuer
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300) if jwks_url else None

    @classmethod
    def from_env(cls) -> TenantAuth | None:
        mode = os.environ.get("ATLAS_A2A_AUTH", "verify")
        if mode == "off":
            return None
        mgr = os.environ.get("ATLAS_MANAGER_URL", "").rstrip("/")
        url = os.environ.get("ATLAS_A2A_JWKS_URL") or (f"{mgr}/.well-known/jwks.json" if mgr else None)
        return cls(mode, url)

    def _decode(self, token: str) -> dict[str, Any]:
        if self._client is None:
            raise jwt.InvalidTokenError("JWKS URL 미설정")
        key = self._client.get_signing_key_from_jwt(token)
        # aud = agent:{Manager 의 에이전트 id} — 에이전트는 자기 Manager id 를 모를 수 있어 형식만 본다
        claims = jwt.decode(token, key.key, algorithms=["ES256"], issuer=self.issuer, options={"verify_aud": False, "require": ["exp", "iss"]})
        if not str((claims.get("aud") or [""])[0] if isinstance(claims.get("aud"), list) else claims.get("aud", "")).startswith("agent:"):
            raise jwt.InvalidTokenError("aud 형식 오류")
        return claims

    async def check(self, headers: dict[str, str], tenant: str) -> dict[str, Any] | None:
        """통과하면 클레임(없으면 None), 실패하면 (code, message) 를 담은 PermissionError."""
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            if self.mode == "required":
                raise PermissionError(UNAUTHENTICATED, "테넌트 토큰 필요 (Manager agent-proxy 경유)")
            return None
        try:
            claims = await asyncio.to_thread(self._decode, token)
        except jwt.PyJWTError as exc:
            raise PermissionError(UNAUTHENTICATED, f"테넌트 토큰 검증 실패: {exc}") from exc
        if claims.get("atlas_org") != tenant:
            span = trace.get_current_span()
            span.add_event("security.tenant_mismatch", {"atlas.tenant": tenant, "atlas.token_org": str(claims.get("atlas_org"))})
            log.warning("security.tenant_mismatch tenant=%s token_org=%s sub=%s", tenant, claims.get("atlas_org"), claims.get("sub"))
            raise PermissionError(TENANT_MISMATCH, "요청 tenant 와 토큰 조직이 다름")
        return claims
