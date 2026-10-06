"""플랫폼 이벤트 발행 (런타임은 NATS 클라이언트가 없다) — Manager `POST /api/v1/events` 중계로 PLT 스트림에 싣는다.
허용 subject: plt.bl.* · plt.ev.* · plt.mat.* (Manager 가 강제). 실패해도 호출 경로를 막지 않는다."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from atlas_agent_core.a2a_client import TokenProvider

log = logging.getLogger("atlas.events")
transport: httpx.AsyncBaseTransport | None = None   # 시험용
sent: list[tuple[str, dict[str, Any]]] = []          # 시험·디버그용 최근 발행 기록


async def publish(spec: Any, subject: str, data: dict[str, Any], tenant: str = "default") -> bool:
    sent.append((subject, data))
    del sent[:-200]
    mgr = (spec.resources.get("manager_url") if spec else None) or os.environ.get("ATLAS_MANAGER_URL", "")
    if not mgr:
        return False
    reg = (spec.resources.get("registration") if spec else None) or {}
    try:
        tok = await TokenProvider(mgr, os.environ.get("ATLAS_MANAGER_TOKEN"), reg, transport)()
        async with httpx.AsyncClient(timeout=3.0, transport=transport) as c:
            res = await c.post(f"{mgr.rstrip('/')}/api/v1/events", params={"org": tenant}, json={"subject": subject, "data": data},
                               headers={"Authorization": f"Bearer {tok}"} if tok else {})
        return res.status_code == 202
    except httpx.HTTPError as exc:
        log.warning("event %s 발행 실패: %s", subject, exc)
        return False
