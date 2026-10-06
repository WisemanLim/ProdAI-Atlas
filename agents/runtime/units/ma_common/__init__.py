"""다중 에이전트(MA-*) 공통 — Gateway LLM 호출, A2A 위임 클라이언트, 위임 명세(DelegationSpec) 검증."""
from __future__ import annotations

import os
from typing import Any

import httpx

from atlas_agent_core import otel
from atlas_agent_core.a2a_client import A2AClient, TokenProvider

# 서브에이전트는 읽기 전용 — 쓰기 성격 도구 이름이 tools_allow 에 있으면 위임 명세를 거부한다 (FR-MA-ORCH-09)
WRITE_MARKERS = ("write", "delete", "create", "update", "deploy", "exec", "drop", "send", "post")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def check_spec(data: dict[str, Any]) -> dict[str, Any]:
    """DelegationSpec{task_id, objective, output_schema_ref, tools_allow[], sources_hint[], boundaries, budget, deadline_s, context_mode}."""
    spec = data.get("spec") or {}
    bad = [t for t in spec.get("tools_allow") or [] if any(m in str(t).lower() for m in WRITE_MARKERS)]
    if bad:
        raise PermissionError(f"서브에이전트는 읽기 전용 — 쓰기 도구 위임 거부: {bad} (FR-MA-ORCH-09)")
    return spec


async def llm(messages: list[dict[str, str]], tags: str, model: str = "chat-default", max_tokens: int = 600) -> tuple[str | None, int]:
    """Gateway 로 모델 호출 (없으면 None — 결정적 대체 경로 사용). 반환: (내용, 토큰)."""
    base = os.environ.get("ATLAS_GATEWAY_URL", "").rstrip("/")
    if not base:
        return None, 0
    headers = otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}", "x-atlas-tags": tags})
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            res = await c.post(f"{base}/v1/chat/completions", json=body, headers=headers)
        res.raise_for_status()
        d = res.json()
        u = d.get("usage") or {}
        return d["choices"][0]["message"]["content"], int(u.get("total_tokens", 0))
    except (httpx.HTTPError, KeyError, ValueError):
        return None, 0


def client_for(agent: Any, tenant: str) -> A2AClient:
    """자기 에이전트 토큰으로 Manager 에 인증하는 위임 클라이언트 (FR-A2A-CLI-03)."""
    mgr = agent.resources.get("manager_url") or os.environ.get("ATLAS_MANAGER_URL", "")
    tp = TokenProvider(mgr, os.environ.get("ATLAS_MANAGER_TOKEN"), agent.resources.get("registration", {}))
    return A2AClient(mgr, tp, tenant=tenant, engine=agent.resources.get("engine"),
                     max_depth=int(os.environ.get("ATLAS_MA_MAX_DEPTH", "2")))


def depth_of(history: list[dict[str, Any]]) -> int:
    """들어온 메시지 metadata 의 x-atlas-depth (FR-MA-ORCH-10)."""
    for m in reversed(history or []):
        md = m.get("metadata") or {}
        if "x-atlas-depth" in md:
            try:
                return int(md["x-atlas-depth"])
            except (TypeError, ValueError):
                return 0
    return 0
