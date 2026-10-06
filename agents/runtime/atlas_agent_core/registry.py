"""Manager 자가 등록 + 배포 등록 (FR-PLT-MGR-01 · PLT-DEP 클라이언트 측).

Manager 가 늦게 뜨거나 멈춰도 에이전트는 계속 동작한다(fail-static, PRD §1.3-3) — 백그라운드 재시도만 한다.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("atlas.registry")


async def register(client: httpx.AsyncClient, manager_url: str, project: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """같은 이름이 있으면 PATCH(새 리비전 또는 no-op), 없으면 POST. 등록된 에이전트를 돌려준다."""
    base = manager_url.rstrip("/") + "/api/v1/agents"
    name, unit = manifest["metadata"]["name"], manifest["metadata"]["unitId"]
    res = await client.get(base, params={"project": project, "unit": unit})
    res.raise_for_status()
    existing = next((a for a in res.json()["items"] if a["name"] == name), None)
    if existing:
        res = await client.patch(f"{base}/{existing['id']}", json=manifest)
        res.raise_for_status()
        return res.json()["agent"]
    res = await client.post(base, params={"project": project}, json=manifest)
    res.raise_for_status()
    return res.json()


async def deploy(client: httpx.AsyncClient, manager_url: str, agent_id: str, endpoint: str, env: str, driver: str) -> dict[str, Any]:
    """이 인스턴스를 배포로 등록한다(같은 agent·env 는 Manager 가 멱등 갱신). 이후 상태는 Manager 가 /readyz 폴링으로 수집."""
    res = await client.post(manager_url.rstrip("/") + "/api/v1/deployments",
                            json={"agentId": agent_id, "env": env, "driver": driver, "endpoint": endpoint})
    res.raise_for_status()
    return res.json()


async def register_forever(manager_url: str, project: str, manifest: dict[str, Any], state: dict[str, Any],
                           *, endpoint: str | None = None, env: str = "dev", driver: str = "compose",
                           max_delay: float = 30.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
    delay = 1.0
    # ATLAS_AUTH_MODE=oidc 인 Manager 는 서비스 API 키(atl_…)로 인증한다 (FR-PLT-IAM-06)
    token = os.environ.get("ATLAS_MANAGER_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token and "CHANGE_ME" not in token else {}
    async with httpx.AsyncClient(timeout=5.0, transport=transport, headers=headers) as client:
        while True:
            try:
                agent = await register(client, manager_url, project, manifest)
                state.update(registered=True, agent_id=agent["id"], rev=agent["currentRev"], error=None)
                log.info("registered with manager: %s rev %s", agent["id"], agent["currentRev"])
                if endpoint:
                    dep = await deploy(client, manager_url, agent["id"], endpoint, env, driver)
                    state.update(deployment_id=dep["id"], env=env)
                    log.info("deployment registered: %s (%s)", dep["id"], env)
                return
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                state.update(registered=False, error=f"{type(exc).__name__}: {exc}")
                log.warning("manager registration failed (retry in %.0fs): %s", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
