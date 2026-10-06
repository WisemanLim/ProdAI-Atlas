"""프롬프트 레지스트리 클라이언트 (FR-BL-HARN-01) — BL `GET /v1/prompts/{name}?label=` 을 로컬 캐시(TTL 60초, stale-while-revalidate)로.

    p = await prompts.get("al-plan.system", label="production", fallback=SYSTEM_PROMPT)
    p.render(goal=...)   # {{var}} 치환 · 현재 span 에 gen_ai.prompt.name/version 기록

BL 이 없거나 응답이 없으면 fallback(코드 내장본, version 0).
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry import trace

TTL_S = 60.0
HARNESS_TTL_S = 10.0   # 하네스는 짧게 — 게시 후 빠르게 반영
transport: httpx.AsyncBaseTransport | None = None
_cache: dict[tuple[str, str], tuple[Prompt, float]] = {}
# 요청 단위 버전 고정 (게시 게이트가 후보 버전을 평가할 때 — A2A metadata atlas.prompts / atlas.harness: {name: version})
_pins: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar("atlas_prompt_pins", default=None)
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass
class Prompt:
    name: str
    version: int
    template: str
    label: str = "production"
    model_config: dict[str, Any] = field(default_factory=dict)

    def render(self, **vars: Any) -> str:
        span = trace.get_current_span()
        span.set_attribute("gen_ai.prompt.name", self.name)
        span.set_attribute("gen_ai.prompt.version", self.version)
        return _VAR.sub(lambda m: str(vars.get(m.group(1), m.group(0))), self.template)


def pin(values: dict[str, Any] | None, kind: str = "prompt") -> contextvars.Token:
    clean = {f"{kind}:{k}": int(v) for k, v in (values or {}).items() if isinstance(v, int) and v > 0}
    return _pins.set({**(_pins.get() or {}), **clean})


def unpin(tok: contextvars.Token) -> None:
    _pins.reset(tok)


async def _get_json(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    url = os.environ.get("ATLAS_BL_URL", "")
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.0, transport=transport) as c:
            res = await c.get(f"{url.rstrip('/')}{path}", params=params)
        return res.json() if res.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def _fetch(name: str, label: str) -> Prompt | None:
    url = os.environ.get("ATLAS_BL_URL", "")
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.0, transport=transport) as c:
            res = await c.get(f"{url.rstrip('/')}/v1/prompts/{name}", params={"label": label})
        if res.status_code != 200:
            return None
        b = res.json()
        return Prompt(name=name, version=int(b["version"]), template=b["template"], label=label, model_config=b.get("model_config") or {})
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def get(name: str, label: str = "production", fallback: str | None = None) -> Prompt:
    if v := (_pins.get() or {}).get(f"prompt:{name}"):
        b = await _get_json(f"/v1/prompts/{name}", {"version": v})
        if b:
            return Prompt(name=name, version=int(b["version"]), template=b["template"], label=f"pinned:{v}", model_config=b.get("model_config") or {})
    key = (name, label)
    hit = _cache.get(key)
    if hit:
        p, at = hit
        if time.monotonic() - at > TTL_S:          # stale-while-revalidate: 옛 값을 주고 뒤에서 갱신
            async def refresh() -> None:
                if (np := await _fetch(name, label)) is not None:
                    _cache[key] = (np, time.monotonic())
            asyncio.get_running_loop().create_task(refresh())
        return p
    p = await _fetch(name, label)
    if p is not None:
        _cache[key] = (p, time.monotonic())
        return p
    return Prompt(name=name, version=0, template=fallback or "", label=label)


def clear_cache() -> None:
    _cache.clear()
    _hcache.clear()


_hcache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}


async def harness(agent: str, label: str = "production") -> dict[str, Any]:
    """하네스 매니페스트 (FR-BL-HARN-03) — loop.max_turns · planner.enabled · tools[] 등. 없으면 {} (코드·env 기본값)."""
    if v := (_pins.get() or {}).get(f"harness:{agent}"):
        b = await _get_json(f"/v1/harness/{agent}", {"version": v})
        return (b or {}).get("manifest") or {}
    hit = _hcache.get((agent, label))
    if hit and time.monotonic() - hit[1] < HARNESS_TTL_S:
        return hit[0]
    b = await _get_json(f"/v1/harness/{agent}", {"label": label})
    m = (b or {}).get("manifest") or {}
    _hcache[(agent, label)] = (m, time.monotonic())
    return m
