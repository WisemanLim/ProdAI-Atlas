"""MCP-REG 시드 — 기본 MCP 서버를 등록(스캔)·승인(도구 고정)하고 테넌트 허용목록을 만든다.
사용: python -m units.mcp_reg.seed   (환경: ATLAS_MCP_REG_ADMIN_TOKEN, MCP_SEED_SERVERS, MCP_SEED_TENANTS)"""
from __future__ import annotations

import json
import os
import sys

import httpx

DEFAULT = [
    {"name": "io.prodai/demo", "version": "1.0.0", "description": "ProdAI Atlas 데모 도구", "trust": "verified",
     "remotes": [{"type": "streamable-http", "url": "http://mcp-demo:8000/mcp"}]},
    {"name": "io.prodai/manager-api", "version": "1.0.0", "description": "Manager REST API (OpenAPI→MCP)", "trust": "verified",
     "remotes": [{"type": "streamable-http", "url": "http://mcp-tool:8000/mcp"}]},
]
# --systems: M3 2차 시스템 서버 (표준 커넥터 팩 · 격리 stdio 데모) — 기본 시드(데모·Manager API)에 더한다
SYSTEMS = [
    {"name": "io.prodai/connectors", "version": "1.0.0", "description": "표준 커넥터 팩 (GitHub·Slack·Jira·Web·ERP DB)", "trust": "official",
     "remotes": [{"type": "streamable-http", "url": "http://agent-mcp-sys:8000/connectors/mcp"}]},
    {"name": "io.prodai/stdio-echo", "version": "1.0.0", "description": "서드파티 stdio 데모 — mcp-sbx 격리 실행", "trust": "community",
     "packages": [{"registryType": "pypi", "identifier": "stdio-demo", "version": "1.0.0", "transport": {"type": "stdio"}}]},
]


def main() -> int:
    base = os.environ.get("MCP_REG_URL", "http://127.0.0.1:8000")
    h = {"authorization": f"Bearer {os.environ['ATLAS_MCP_REG_ADMIN_TOKEN']}"}
    servers = json.loads(os.environ["MCP_SEED_SERVERS"]) if os.environ.get("MCP_SEED_SERVERS") else DEFAULT + (SYSTEMS if "--systems" in sys.argv else [])
    rc = 0
    with httpx.Client(base_url=base, headers=h, timeout=30) as c:
        for s in servers:
            trust = s.pop("trust", "community")
            r = c.post("/admin/servers", json=s)
            status = r.json().get("_meta", {}).get("io.prodai.atlas/registry", {}).get("status") if r.is_success else r.text[:120]
            print(f"register {s['name']}@{s['version']}: {r.status_code} {status}")
            if status != "scanned":
                rc = 1
                continue
            a = c.post(f"/admin/servers/{s['name']}/versions/{s['version']}/approve", json={"trust": trust})
            print(f"approve  {s['name']}: {a.status_code} pins={len(a.json().get('pins', {})) if a.is_success else a.text[:120]}")
            rc |= not a.is_success
        allowed = [{"name": s["name"], "version": "*"} for s in servers]
        for tenant in os.environ.get("MCP_SEED_TENANTS", "default").split(","):
            c.put(f"/admin/allowlist/{tenant}", json={"servers": allowed, "tools": {"include": ["*"], "exclude": []}}).raise_for_status()
            print(f"allowlist {tenant}: {len(allowed)} servers")
    return rc


if __name__ == "__main__":
    sys.exit(main())
