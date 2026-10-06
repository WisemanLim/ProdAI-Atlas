"""FastAPI 앱 조립 — Atlas Agent Spec 공통 엔드포인트 (PRD §11.13)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Engine, text

from atlas_agent_core import otel, registry
from atlas_agent_core import push as push_mod
from atlas_agent_core import secrets as secrets_mod
from atlas_agent_core.a2a import PARSE_ERROR, A2AHandler, RpcError, error_body
from atlas_agent_core.card import build_card
from atlas_agent_core.spec import AgentSpec
from atlas_agent_core.tasks import TaskStore
from atlas_agent_core.tenancy import TenantAuth

log = logging.getLogger("atlas.server")
CORE_VERSION = "0.2.0"  # x-release-please-version — prodai-agent-core (A2A·헬스·OTel 공통 런타임) — compat.json 의 agentCore 범위와 비교
PROBE_PATHS = frozenset({"/healthz", "/readyz", "/health", "/.well-known/agent-card.json"})


def skip_trace(scope: dict) -> bool:
    """프로브 경로와, 호출자가 추적을 요청하지 않은(traceparent 없는) GET 은 span 을 만들지 않는다 — 콘솔 폴링 노이즈 제거.
    상태를 바꾸는 요청(POST 등)과 traceparent 가 있는 GET 은 추적한다."""
    if scope.get("path") in PROBE_PATHS:
        return True
    if scope.get("type") == "http" and scope.get("method") == "GET":
        return not any(k == b"traceparent" for k, _ in scope.get("headers", []))
    return False


def create_app(spec: AgentSpec, *, engine: Engine, public_url: str, env: str = "local", db_engine: str = "sqlite",
               manager_url: str | None = None, project: str = "default", deploy_env: str = "dev", driver: str = "compose",
               on_startup: list[Any] | None = None) -> FastAPI:
    otel.init(f"atlas-agent-{spec.name}", spec.unit_id, env)
    spec.resources.setdefault("engine", engine)
    store = TaskStore(engine)
    card = build_card(spec, public_url)
    signer = push_mod.Signer()
    dispatcher = push_mod.Dispatcher(push_mod.PushStore(engine), signer, issuer=public_url.rstrip("/"))
    handler = A2AHandler(spec, store, TenantAuth.from_env(), push=dispatcher, card=card)
    reg_state: dict[str, Any] = {"registered": False, "enabled": bool(manager_url)}
    spec.resources.setdefault("registration", reg_state)   # 단위가 자기 agent_id(위임 토큰)를 알도록
    spec.resources.setdefault("manager_url", manager_url)
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for fn in on_startup or []:
            fn()
        job = None
        if manager_url:
            job = asyncio.create_task(registry.register_forever(manager_url, project, spec.manifest, reg_state,
                                                                endpoint=public_url, env=deploy_env, driver=driver))
        # 비밀 주입 (FR-PLT-SEC-03): 매니페스트 spec.secrets[] 선언분만 Manager 중개로 받는다
        declared = [s["name"] for s in (spec.manifest.get("spec", {}).get("secrets") or []) if isinstance(s, dict) and s.get("name")]
        secrets_mod.client = secrets_mod.SecretClient(manager_url, os.environ.get("ATLAS_MANAGER_TOKEN"), declared,
                                                      refresh_s=float(os.environ.get("ATLAS_SECRET_REFRESH_S", "300")))
        secrets_mod.client_state = reg_state
        sjob = asyncio.create_task(secrets_mod.client.run(reg_state)) if declared and manager_url else None
        bg = [asyncio.create_task(fn()) for fn in spec.background]
        async with contextlib.AsyncExitStack() as stack:
            for _, sub in spec.mounts:          # 마운트한 하위 앱(fastmcp 등)의 lifespan
                if getattr(sub, "router", None) is not None and hasattr(sub.router, "lifespan_context"):
                    await stack.enter_async_context(sub.router.lifespan_context(sub))
            log.info("%s (%s) ready in %.2fs", spec.name, spec.unit_id, time.monotonic() - started)
            yield
        for t in [job, sjob, *bg]:
            if t:
                t.cancel()

    # FastAPI 내장 텔레메트리: 프로브 경로 제외, 의존성·직렬화 operation span 끔(저장량), OTLP 구성은 atlas otel.init 이 담당
    telemetry = {"operation_spans": False, "metrics": False, "logs": False, "auto_configure": False, "exclude": skip_trace}
    app = FastAPI(title=f"atlas-agent {spec.name}", version=spec.version, lifespan=lifespan, telemetry=telemetry)
    app.state.spec, app.state.store, app.state.registration = spec, store, reg_state

    for router in spec.routers:
        app.include_router(router)
    for path, sub in spec.mounts:
        app.mount(path, sub)

    @app.get("/.well-known/agent-card.json")
    async def agent_card():
        return card

    @app.get("/.well-known/jwks.json")
    async def jwks():  # push webhook 서명 공개키 (FR-A2A-SRV-03)
        return signer.jwks()

    @app.post("/a2a")
    async def a2a(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(error_body(None, RpcError(PARSE_ERROR, "parse error")))
        if isinstance(body, dict) and body.get("method") in ("SendStreamingMessage", "SubscribeToTask"):
            async def sse():
                async for msg in handler.stream(body, dict(request.headers)):
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        return JSONResponse(await handler.dispatch(body, dict(request.headers)))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    async def readiness() -> tuple[int, dict[str, Any]]:
        checks: dict[str, str] = {}
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["db"] = f"error: {type(exc).__name__}"
        for chk in spec.readiness_checks:
            try:
                await chk()
                checks[chk.__name__] = "ok"
            except Exception as exc:  # noqa: BLE001
                checks[chk.__name__] = f"error: {type(exc).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return (200 if ok else 503), {"status": "ok" if ok else "degraded", "env": env, "unit": spec.unit_id,
                                      "version": spec.version, "agent_core": CORE_VERSION,
                                      "db": {"engine": db_engine, "status": checks["db"]}, "checks": checks,
                                      "registration": reg_state}

    @app.get("/readyz")
    async def readyz():
        code, body = await readiness()
        return JSONResponse(body, status_code=code)

    @app.get("/health")  # 스캐폴드 compose 헬스체크 호환
    async def health():
        code, body = await readiness()
        return JSONResponse(body, status_code=code)

    return app
