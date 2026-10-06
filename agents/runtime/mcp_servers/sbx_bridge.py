"""mcp-sbx — 저신뢰 stdio MCP 서버를 격리 프로세스로 띄우고 Streamable HTTP 로 중계한다 (FR-X-SBX-07 · FR-MCP-SYS-03).

  /servers/{slug}/mcp   slug = 레지스트리 이름의 '/' → '--' (예: io.prodai--stdio-echo)
  GET /servers          허용 패키지·권한 프로필

- 실행 가능한 것은 이미지에 번들된 패키지만(MCP_SBX_PACKAGES: 이름=모듈, 임의 명령 금지).
- 권한 프로필(permissions, ToolHive 방식) MCP_SBX_PROFILES: {이름: {network: {allow:[호스트]}, env:[통과 키]}}:
  환경변수는 비우고 허용 키만, 네트워크는 egress 프록시 정책(허용목록)만, rlimit(CPU·메모리·프로세스·파일), 컨테이너는 read-only·cap_drop ALL.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys

import httpx
import uvicorn
from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 번들 패키지 루트 (/app)
PACKAGES = dict(p.split("=", 1) for p in os.environ.get("MCP_SBX_PACKAGES", "io.prodai/stdio-echo=mcp_servers.stdio_demo").split(",") if "=" in p)
PROFILES = json.loads(os.environ.get("MCP_SBX_PROFILES", '{"io.prodai/stdio-echo": {"network": {"allow": ["fake-upstream"]}, "env": []}}'))
LIMITER = ("import os,resource,sys\n"
           "for k,v in ((resource.RLIMIT_CPU,600),(resource.RLIMIT_AS,512<<20),(resource.RLIMIT_NPROC,64),(resource.RLIMIT_NOFILE,128),(resource.RLIMIT_CORE,0)):\n"
           "    resource.setrlimit(k,(v,v))\n"
           "boot='import runpy,sys;sys.path.insert(0,sys.argv[2]);runpy.run_module(sys.argv[1],run_name=\"__main__\")'\n"
           "os.execv(sys.executable,[sys.executable,'-I','-c',boot,sys.argv[1],sys.argv[2]])\n")   # -I: PYTHONPATH·사용자 site 무시 → 번들 경로만 명시


def slug(name: str) -> str:
    return name.replace("/", "--")


async def egress_env(name: str) -> dict[str, str]:
    """프로필 허용목록으로 egress 정책 등록 → 자식 프로세스는 프록시로만 나간다."""
    allow = (PROFILES.get(name) or {}).get("network", {}).get("allow") or []
    if not allow:
        return {}
    pid, tok = f"mcp-sbx-{slug(name)}", secrets.token_urlsafe(16)
    admin = os.environ.get("ATLAS_EGRESS_ADMIN_URL", "http://egress-proxy:8080").rstrip("/")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.put(f"{admin}/policies/{pid}", headers={"X-Egress-Admin": os.environ.get("EGRESS_ADMIN_TOKEN", "")},
                        json={"token": tok, "allow": allow, "ttl_s": 86400 * 30})
        r.raise_for_status()
    pu = os.environ.get("ATLAS_EGRESS_PROXY_URL", "http://egress-proxy:3128").replace("://", f"://{pid}:{tok}@", 1)
    return {"HTTP_PROXY": pu, "HTTPS_PROXY": pu, "http_proxy": pu, "https_proxy": pu}


async def build() -> tuple[list, list]:
    routes, lifespans = [Route("/servers", lambda r: JSONResponse({"packages": PACKAGES, "profiles": PROFILES})),
                         Route("/health", lambda r: JSONResponse({"status": "ok"}))], []
    for name, module in PACKAGES.items():
        prof = PROFILES.get(name) or {}
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONPATH": "/app"}
        env.update({k: os.environ[k] for k in prof.get("env", []) if k in os.environ})
        for _ in range(10):
            try:
                env.update(await egress_env(name))
                break
            except httpx.HTTPError:
                await asyncio.sleep(2)
        transport = StdioTransport(sys.executable, ["-I", "-c", LIMITER, module, APP_DIR], env=env, cwd="/tmp")
        proxy = create_proxy(transport, name=f"sbx-{slug(name)}")   # transport 를 넘겨야 ProxyClient — 진행률·입력 요청이 요청 문맥으로 중계된다
        sub = proxy.http_app(path="/mcp")
        routes.append(Mount(f"/servers/{slug(name)}", app=sub))
        lifespans.append(sub)
    return routes, lifespans


def main() -> None:
    routes, subs = asyncio.run(build())

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as st:
            for s in subs:
                await st.enter_async_context(s.router.lifespan_context(s))
            yield

    uvicorn.run(Starlette(routes=routes, lifespan=lifespan), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
