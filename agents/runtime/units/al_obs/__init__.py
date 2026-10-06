"""AL-OBS Observer — 도구 결과·오류를 호출과 짝지은 표준 관찰로 정규화 (FR-AL-OBS-01·02·03)."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

OK, ERROR, TIMEOUT, DENIED = "ok", "error", "timeout", "denied"


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


@dataclass
class Observation:
    tool_call_id: str
    tool: str
    status: str                      # ok | error | timeout | denied
    content: str                     # 모델에 돌려줄 본문(잘린 경우 head/tail)
    error: dict[str, Any] | None = None   # {error_type, message, hint, retryable}
    resource_link: str | None = None      # 잘린 원문 위치 (FR-AL-OBS-03)
    bytes: int = 0
    tokens: int = 0
    replayed: bool = False                # 멱등 키로 재실행 생략됨
    meta: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """OpenAI role=tool 메시지. 오류는 숨기지 않고 원인·힌트를 JSON 으로 (FR-AL-OBS-02)."""
        body = self.content
        if self.status != OK:
            body = json.dumps({"is_error": True, "status": self.status, **(self.error or {})}, ensure_ascii=False)
        elif self.resource_link:
            body = f"{self.content}\n[truncated — full result: {self.resource_link}]"
        return {"role": "tool", "tool_call_id": self.tool_call_id, "content": body}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def error(tool_call_id: str, tool: str, status: str, error_type: str, message: str, hint: str = "",
          retryable: bool = False) -> Observation:
    err = {"error_type": error_type, "message": message, "hint": hint, "retryable": retryable}
    return Observation(tool_call_id, tool, status, "", error=err, tokens=estimate_tokens(message))


def normalize(tool_call_id: str, tool: str, result: Any, *, max_tokens: int,
              store: Callable[[str], str] | None = None) -> Observation:
    """성공 결과를 문자열로 정규화하고, max_tokens 를 넘으면 head/tail 만 남기고 원문은 store 에 맡긴다."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    raw_bytes = len(text.encode())
    if estimate_tokens(text) <= max_tokens:
        return Observation(tool_call_id, tool, OK, text, bytes=raw_bytes, tokens=estimate_tokens(text))
    keep = max_tokens * 4 // 2
    link = store(text) if store else None
    clipped = f"{text[:keep]}\n…[{len(text) - 2 * keep} chars omitted]…\n{text[-keep:]}"
    return Observation(tool_call_id, tool, OK, clipped, resource_link=link, bytes=raw_bytes, tokens=estimate_tokens(clipped))
