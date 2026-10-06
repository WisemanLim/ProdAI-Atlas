"""AL-REF 단독 배포 진입점 — ATLAS_UNIT_MODULE=units.al_ref.agent_spec (AL-RUN 은 같은 로직을 프로세스 안에서 합성)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, Skill, TaskContext
from units.al_ref import assess_final


async def reflect(ctx: TaskContext) -> dict[str, Any]:
    v = assess_final(str(ctx.data.get("output", ctx.text)), ctx.data.get("dod") or [])
    return {"decision": v.decision, "reasons": v.reasons, "dod": [r.__dict__ for r in v.dod]}


agent = AgentSpec.from_file(Path(__file__).with_name("atlas-agent.json"), description="AL-REF — 최종 출력에 DoD 를 적용해 계속/수정/완료 후보 판정",
                            skills=[Skill(id="loop.reflect", name="Reflect", handler=reflect, description="최종 출력에 DoD 를 적용해 계속/수정/완료 후보 판정")])
