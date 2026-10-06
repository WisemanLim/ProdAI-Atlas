"""AL-ACT 단독 배포 진입점 — ATLAS_UNIT_MODULE=units.al_act.agent_spec (AL-RUN 은 같은 로직을 프로세스 안에서 합성)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from units.al_act import BUILTINS, ActConfig, Actor, ApprovalNeeded


async def act(ctx: TaskContext) -> dict[str, Any]:
    calls = ctx.data.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise InputRequired("data.tool_calls(OpenAI 형식) 가 필요합니다")
    actor = Actor({t.name: t for t in BUILTINS}, ActConfig(permission_mode=ctx.data.get("permission_mode", "default")))
    try:
        obs = await actor.execute(calls, run_id=ctx.task_id, step=0, ctx={})
    except ApprovalNeeded as need:
        raise InputRequired("승인 필요: " + "; ".join(need.reasons)) from None
    return {"observations": [o.to_dict() for o in obs]}


agent = AgentSpec.from_file(Path(__file__).with_name("atlas-agent.json"), description="AL-ACT — 도구 호출을 검증·권한 확인 후 실행 (내장 도구)",
                            skills=[Skill(id="loop.act", name="Act", handler=act, description="도구 호출을 검증·권한 확인 후 실행 (내장 도구)")])
