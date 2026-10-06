"""AL-EXIT 단독 배포 진입점 — ATLAS_UNIT_MODULE=units.al_exit.agent_spec (AL-RUN 은 같은 로직을 프로세스 안에서 합성)."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from units.al_exit import ExitConfig, check_budget


async def decide(ctx: TaskContext) -> dict[str, Any]:
    state = ctx.data.get("state")
    if not isinstance(state, dict):
        raise InputRequired("data.state(turns·llm_calls·usage·cost_usd·started_at) 가 필요합니다")
    cfg = ExitConfig(**(ctx.data.get("config") or {}))
    d = check_budget(cfg, state, time.time())
    return {"exit": bool(d), "reason": d.reason if d else None, "status": d.status if d else "running", "detail": d.detail if d else ""}


agent = AgentSpec.from_file(Path(__file__).with_name("atlas-agent.json"), description="AL-EXIT — 런 상태로 종료 조건(상한·예산·시간) 판정",
                            skills=[Skill(id="loop.exit", name="Exit", handler=decide, description="런 상태로 종료 조건(상한·예산·시간) 판정")])
