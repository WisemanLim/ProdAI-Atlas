"""AL-OBS 단독 배포 진입점 — ATLAS_UNIT_MODULE=units.al_obs.agent_spec (AL-RUN 은 같은 로직을 프로세스 안에서 합성)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from units.al_obs import normalize


async def observe(ctx: TaskContext) -> dict[str, Any]:
    d = ctx.data
    if "tool_call_id" not in d:
        raise InputRequired("tool_call_id·tool·result 가 필요합니다")
    o = normalize(d["tool_call_id"], d.get("tool", ""), d.get("result", ""), max_tokens=int(d.get("max_tokens", 8000)))
    return {"observation": o.to_dict(), "message": o.to_message()}


agent = AgentSpec.from_file(Path(__file__).with_name("atlas-agent.json"), description="AL-OBS — 도구 결과를 표준 Observation 으로 정규화",
                            skills=[Skill(id="loop.observe", name="Observe", handler=observe, description="도구 결과를 표준 Observation 으로 정규화")])
