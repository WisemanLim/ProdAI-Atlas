"""보정 로직 플래그 (FR-BL-HARN-04) — OpenFeature 원격 평가 프로토콜(OFREP) 로 flagd 에 묻는다.

    if await flags.enabled("harness.plan_json_repair.enabled", default=True): ...

- 플래그 정본은 BL 서비스가 쓰는 flagd 파일(도입 이유·도입 모델·제거 조건 메타데이터 필수).
- 요청 단위 덮어쓰기: A2A 메시지 metadata `atlas.flags` (ablation 평가가 쓴다, `harness.` 키만 허용).
- flagd 가 없거나 응답이 없으면 default (fail-static). 결과는 5초 캐시.
"""
from __future__ import annotations

import contextvars
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

_overrides: contextvars.ContextVar[dict[str, bool] | None] = contextvars.ContextVar("atlas_flag_overrides", default=None)
_cache: dict[str, tuple[bool, float]] = {}
TTL_S = 5.0
transport: httpx.AsyncBaseTransport | None = None   # 시험용


@contextmanager
def overrides(values: dict[str, Any] | None) -> Iterator[None]:
    clean = {k: bool(v) for k, v in (values or {}).items() if isinstance(k, str) and k.startswith("harness.")}
    tok = _overrides.set({**(_overrides.get() or {}), **clean})
    try:
        yield
    finally:
        _overrides.reset(tok)


def active_overrides() -> dict[str, bool]:
    return dict(_overrides.get() or {})


async def enabled(key: str, default: bool = True, context: dict[str, Any] | None = None) -> bool:
    ov = _overrides.get() or {}
    if key in ov:
        return ov[key]
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[1] < TTL_S:
        return hit[0]
    url = os.environ.get("ATLAS_FLAGD_URL", "")
    if not url:
        return default
    try:
        async with httpx.AsyncClient(timeout=1.5, transport=transport) as c:
            res = await c.post(f"{url.rstrip('/')}/ofrep/v1/evaluate/flags/{key}", json={"context": context or {"targetingKey": "atlas"}})
        if res.status_code != 200:
            return default
        val = bool(res.json().get("value", default))
    except (httpx.HTTPError, ValueError):
        return default
    _cache[key] = (val, time.monotonic())
    return val


def clear_cache() -> None:
    _cache.clear()
