"""저신뢰 서드파티 stdio MCP 서버 예 (시험용) — mcp-sbx 브리지가 격리 프로세스로 띄운다 (FR-X-SBX-07 · MCP-SYS-03).
도구: echo · whoami(환경 노출 점검) · fetch(네트워크 시도 — 프로필 허용목록 밖은 막혀야 한다) · write_file(읽기 전용 파일 시스템 점검)."""
from __future__ import annotations

import os
import urllib.request

from fastmcp import FastMCP

server = FastMCP("stdio-demo", instructions="격리 시험용 서드파티 stdio 서버")


@server.tool(annotations={"readOnlyHint": True})
def echo(text: str) -> str:
    """받은 문자열을 돌려준다."""
    return text


@server.tool(annotations={"readOnlyHint": True})
def whoami() -> dict:
    """프로세스 환경 요약 (비밀 환경변수가 새지 않아야 한다)."""
    return {"uid": os.getuid(), "env_keys": sorted(os.environ), "cwd": os.getcwd()}


@server.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def fetch(url: str) -> dict:
    """URL 을 가져온다 (egress 프록시 허용목록 안에서만 성공)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return {"ok": True, "status": r.status, "text": r.read(200).decode(errors="replace")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


@server.tool(annotations={"readOnlyHint": False})
def write_file(path: str, text: str) -> dict:
    """파일을 쓴다 (읽기 전용 루트에서는 실패해야 한다)."""
    try:
        with open(path, "w") as f:
            f.write(text)
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}


if __name__ == "__main__":
    server.run(transport="stdio")
