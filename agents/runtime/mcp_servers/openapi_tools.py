"""MCP-TOOL — OpenAPI 3.x 명세를 MCP 도구로 노출 (FR-MCP-TOOL-01·03).

기본 대상은 Manager API(`MCP_OPENAPI_URL`). 안전 기본값: GET 만 도구로(`MCP_OPENAPI_METHODS` 로 확장), 모든 도구에
title·annotations(readOnlyHint/destructiveHint/idempotentHint)를 채운다.
"""
from __future__ import annotations

import os
import re

import httpx2
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

SPEC_URL = os.environ.get("MCP_OPENAPI_URL", "http://manager:8080/api/v1/openapi.json")
BASE_URL = os.environ.get("MCP_OPENAPI_BASE_URL", "http://manager:8080")
METHODS = [m.strip().upper() for m in os.environ.get("MCP_OPENAPI_METHODS", "GET").split(",") if m.strip()]
EXCLUDE = ("/health", "/api/v1/openapi.json")
# 콘솔용 내부 서비스 프록시·조직·IAM 관리 API — 에이전트 도구로 노출하면 플랫폼 관리 권한이 새어 나간다
EXCLUDE_PREFIX = ("/svc/", "/api/v1/orgs", "/api/v1/auth", "/api/v1/secrets", "/api/v1/approvals", "/.well-known")
# 배포 제어면(롤아웃·토폴로지·매니페스트 렌더)은 운영자 API — 에이전트 도구로 내보내지 않는다
EXCLUDE_RE = (r"^/api/v1/rollouts", r"^/api/v1/topology", r"/rollouts$", r"/manifests$", r"^/scim/", r"^/api/v1/plans", r"^/api/v1/token/")   # 배포·IAM 관리 경로는 도구화하지 않음


def annotate(route, component) -> None:
    """도구 메타 표준화 — HTTP 메서드로 주석을 정하고, operationId 가 없으면 메서드·경로로 이름을 만든다."""
    method = route.method.upper()
    if not route.operation_id:
        component.name = re.sub(r"[^a-zA-Z0-9_]+", "_", f"{method.lower()}_{route.path}").strip("_")
    component.title = component.title or (route.summary or route.operation_id or f"{method} {route.path}")[:80]
    component.annotations = {"readOnlyHint": method == "GET", "destructiveHint": method == "DELETE",
                             "idempotentHint": method in ("GET", "PUT", "DELETE"), "openWorldHint": False}


def build(spec: dict, client: httpx2.AsyncClient) -> FastMCP:
    maps = [RouteMap(pattern=f"^{p}$", mcp_type=MCPType.EXCLUDE) for p in EXCLUDE]
    maps += [RouteMap(pattern=f"^{p}", mcp_type=MCPType.EXCLUDE) for p in EXCLUDE_PREFIX]
    maps += [RouteMap(pattern=p, mcp_type=MCPType.EXCLUDE) for p in EXCLUDE_RE]
    maps += [RouteMap(methods=[m], mcp_type=MCPType.TOOL) for m in METHODS]
    maps.append(RouteMap(mcp_type=MCPType.EXCLUDE))   # 그 외 전부 제외
    return FastMCP.from_openapi(spec, client=client, name="atlas-openapi", route_maps=maps, mcp_component_fn=annotate)


def main() -> None:
    spec = httpx2.get(SPEC_URL, timeout=10).json()
    token = os.environ.get("ATLAS_MANAGER_TOKEN", "")  # 자기 서비스 키 — 사용자 토큰 패스스루 금지
    headers = {"Authorization": f"Bearer {token}"} if token and "CHANGE_ME" not in token else {}
    server = build(spec, httpx2.AsyncClient(base_url=BASE_URL, timeout=30, headers=headers))
    server.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), path="/mcp")


if __name__ == "__main__":
    main()
