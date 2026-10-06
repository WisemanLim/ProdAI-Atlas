"""ClickHouse HTTP 인터페이스 클라이언트 — 모든 값은 서버측 파라미터({name:Type})로 바인딩한다(문자열 결합 금지)."""
from __future__ import annotations

import os
from typing import Any

import httpx


class ClickHouse:
    def __init__(self, url: str | None = None, user: str | None = None, password: str | None = None,
                 database: str = "otel", transport: httpx.AsyncBaseTransport | None = None):
        self.url = (url or os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")).rstrip("/")
        self.auth = (user or os.environ.get("CLICKHOUSE_USER", "atlas"), password or os.environ.get("CLICKHOUSE_PASSWORD", ""))
        self.database, self.transport = database, transport

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        qp = {"database": self.database, "default_format": "JSON", "readonly": "1"}
        for k, v in (params or {}).items():
            qp[f"param_{k}"] = v if isinstance(v, str) else str(v)
        async with httpx.AsyncClient(timeout=30, transport=self.transport, auth=self.auth) as c:
            res = await c.post(self.url, params=qp, content=sql.encode())
        if res.status_code != 200:
            raise RuntimeError(f"clickhouse {res.status_code}: {res.text[:300]}")
        return res.json()["data"]

    async def command(self, sql: str, params: dict[str, Any] | None = None) -> str:
        """쓰기 명령(변경·mutation) — 삭제권 전파(X-KB-08) 같은 관리 작업만."""
        qp = {"database": self.database}
        for k, v in (params or {}).items():
            qp[f"param_{k}"] = v if isinstance(v, str) else str(v)
        async with httpx.AsyncClient(timeout=120, transport=self.transport, auth=self.auth) as c:
            res = await c.post(self.url, params=qp, content=sql.encode())
        if res.status_code != 200:
            raise RuntimeError(f"clickhouse {res.status_code}: {res.text[:300]}")
        return res.text

    async def ping(self) -> None:
        async with httpx.AsyncClient(timeout=5, transport=self.transport) as c:
            (await c.get(f"{self.url}/ping")).raise_for_status()
