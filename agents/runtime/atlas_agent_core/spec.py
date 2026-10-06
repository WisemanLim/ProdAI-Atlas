"""단위 에이전트 선언 모델."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class InputRequired(Exception):
    """스킬이 추가 입력을 요구할 때 던진다 → Task 상태 INPUT_REQUIRED."""


@dataclass
class TaskContext:
    """스킬 핸들러가 받는 실행 문맥."""

    tenant: str
    task_id: str
    context_id: str
    skill: str
    text: str                      # message.parts 의 text 를 이어 붙인 값
    data: dict[str, Any]           # message.parts 의 data 를 병합한 값
    history: list[dict[str, Any]]  # 지금까지의 메시지
    headers: dict[str, str] = field(default_factory=dict)  # 원 요청 헤더(trace 전파용)


# 핸들러 반환값 = artifact parts 의 data(JSON) 또는 text
Handler = Callable[[TaskContext], Awaitable[dict[str, Any] | str]]


@dataclass
class Skill:
    id: str
    name: str
    description: str
    handler: Handler
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=lambda: ["text/plain", "application/json"])
    output_modes: list[str] = field(default_factory=lambda: ["application/json"])
    security: str = ""  # "required" = 카드 securityRequirements 선언 + 테넌트 토큰 없으면 거부 (FR-A2A-SRV-05)


@dataclass
class AgentSpec:
    """atlas-agent.json(매니페스트, PRD §11.14) + 스킬 핸들러."""

    manifest: dict[str, Any]
    description: str
    skills: list[Skill]
    readiness_checks: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    routers: list[Any] = field(default_factory=list)      # 추가 HTTP 라우트 (FastAPI APIRouter)
    resources: dict[str, Any] = field(default_factory=dict)  # create_app 이 채움: engine 등
    background: list[Callable[[], Awaitable[None]]] = field(default_factory=list)  # 기동 시 시작하는 장기 작업(경보 평가 등)
    mounts: list[tuple[str, Any]] = field(default_factory=list)  # (경로, ASGI 앱) — 예: MCP Streamable HTTP 서버. 하위 앱 lifespan 도 함께 돈다

    @classmethod
    def from_file(cls, path: str | Path, **kw: Any) -> AgentSpec:
        return cls(manifest=json.loads(Path(path).read_text(encoding="utf-8")), **kw)

    @property
    def name(self) -> str:
        return self.manifest["metadata"]["name"]

    @property
    def unit_id(self) -> str:
        return self.manifest["metadata"]["unitId"]

    @property
    def version(self) -> str:
        return self.manifest["spec"]["version"]

    def skill(self, skill_id: str | None) -> Skill | None:
        if not skill_id:
            return self.skills[0]
        return next((s for s in self.skills if s.id == skill_id), None)
