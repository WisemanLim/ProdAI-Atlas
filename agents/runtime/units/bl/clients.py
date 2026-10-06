"""BL 외부 호출 — Gateway 관리 API(alias·카탈로그·통계, 토큰 = 비밀 중개 gateway-admin-token) · EV(회귀 실행·diff)."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from atlas_agent_core import otel, secrets

TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험용


def client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=TRANSPORT)


def gw_url() -> str:
    return os.environ.get("ATLAS_GATEWAY_URL", "http://gateway:8080").rstrip("/")


def ev_url() -> str:
    return os.environ.get("ATLAS_EV_URL", "http://agent-ev:8000").rstrip("/")


async def admin_token() -> str:
    return await secrets.get("gateway-admin-token", env="ATLAS_GATEWAY_ADMIN_TOKEN", dev_default="local-admin-token")


async def gw(method: str, path: str, body: Any = None) -> Any:
    async with client() as c:
        res = await c.request(method, gw_url() + path, json=body, headers={"Authorization": f"Bearer {await admin_token()}", **otel.inject({})})
    if res.status_code >= 400:
        try:
            msg = res.json().get("error", {}).get("message", res.text)
        except ValueError:
            msg = res.text
        raise GatewayError(res.status_code, msg)
    return res.json() if res.content else {}


class GatewayError(RuntimeError):
    def __init__(self, status: int, msg: str):
        super().__init__(f"gateway {status}: {msg}")
        self.status = status


async def catalog() -> list[dict[str, Any]]:
    """Gateway 카탈로그 — 필드명 정규화(deprecation_in_days → deprecation_in, prices_per_1m → prices)."""
    out = []
    for m in (await gw("GET", "/admin/econ/models")).get("models", []):
        m = dict(m)
        if "deprecation_in_days" in m:
            m["deprecation_in"] = m.pop("deprecation_in_days")
        if "prices_per_1m" in m:
            m["prices"] = m.pop("prices_per_1m")
        out.append(m)
    return out


async def aliases() -> list[dict[str, Any]]:
    return (await gw("GET", "/admin/aliases")).get("data", [])


async def alias(name: str) -> dict[str, Any]:
    return await gw("GET", f"/admin/aliases/{name}")


async def put_alias(name: str, deployments: list[dict], fallbacks: list[str] | None = None, note: str = "", by: str = "bl") -> dict[str, Any]:
    return await gw("PUT", f"/admin/aliases/{name}", {"deployments": deployments, "fallbacks": fallbacks or [], "note": note, "by": by})


async def rollback_alias(name: str, version: int, by: str = "bl") -> dict[str, Any]:
    return await gw("POST", f"/admin/aliases/{name}/rollback", {"version": version, "by": by})


async def delete_alias(name: str) -> None:
    await gw("DELETE", f"/admin/aliases/{name}")


async def alias_stats(name: str, version: int | None = None) -> list[dict[str, Any]]:
    q = f"?version={version}" if version is not None else ""
    return (await gw("GET", f"/admin/aliases/{name}/stats{q}")).get("models", [])


async def ev_run(suite: dict[str, Any]) -> dict[str, Any]:
    async with client(600) as c:
        res = await c.post(ev_url() + "/v1/runs", content=json.dumps(suite), headers=otel.inject({"content-type": "application/json"}))
    if res.status_code >= 400:
        raise RuntimeError(f"EV run {res.status_code}: {res.text[:300]}")
    return res.json()


async def ev_diff(base: str, new: str) -> dict[str, Any]:
    async with client() as c:
        res = await c.get(f"{ev_url()}/v1/runs/{base}/diff/{new}")
    res.raise_for_status()
    return res.json()


async def chat(body: dict[str, Any], tags: str = "agent_id:bl,unit_id:BL-COMP") -> tuple[dict[str, Any], dict[str, str]]:
    async with client(120) as c:
        res = await c.post(gw_url() + "/v1/chat/completions", json=body,
                           headers=otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}", "x-atlas-tags": tags}))
    res.raise_for_status()
    return res.json(), {k.lower(): v for k, v in res.headers.items()}
