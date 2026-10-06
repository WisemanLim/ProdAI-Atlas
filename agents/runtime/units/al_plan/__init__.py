"""AL-PLAN Planner — 목표를 순서 있는 단계 계획과 완료 정의(DoD)로 분해 (FR-AL-PLAN-01·02)."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

from atlas_agent_core import (
    AgentSpec,
    InputRequired,
    Skill,
    TaskContext,
    flags,
    otel,
    prompts,
)

MANIFEST = Path(__file__).with_name("atlas-agent.json")
_SPLIT = re.compile(r"\n+|(?<=[.!?。])\s+|\s*(?:;|→|->|그리고|그 다음|그다음|then)\s*", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a planner. Decompose the user's goal into ordered steps and a definition of done. "
    'Reply with JSON only: {"steps":[{"title":str,"depends_on":[int]}],"dod":[{"type":"test|schema|judge|human","spec":str}]}'
)


class PlanInvalid(ValueError):
    """DoD 없는 계획 등 스키마 위반 (FR-AL-PLAN-02: plan.invalid 거부)."""


def make_plan(goal: str, raw_steps: list[dict[str, Any]], dod: list[dict[str, Any]], source: str, max_steps: int) -> dict[str, Any]:
    if not raw_steps:
        raise PlanInvalid("plan.invalid: steps 비어 있음")
    if not dod:
        raise PlanInvalid("plan.invalid: DoD 없음")
    steps = []
    for i, s in enumerate(raw_steps[:max_steps]):
        deps = [f"s{d}" for d in s.get("depends_on", []) if isinstance(d, int) and 1 <= d <= i]
        steps.append({"id": f"s{i + 1}", "title": str(s["title"]).strip(), "status": "pending", "depends_on": deps})
    return {"id": str(uuid.uuid4()), "goal": goal, "steps": steps, "dod": dod, "source": source}


def heuristic_plan(goal: str, max_steps: int) -> dict[str, Any]:
    """게이트웨이 없이 쓰는 결정적 분해 — 문장·접속어 경계로 자르고 순차 의존을 건다."""
    parts = [p.strip(" ,.") for p in _SPLIT.split(goal) if p and p.strip(" ,.")]
    raw = [{"title": p, "depends_on": [i] if i else []} for i, p in enumerate(parts)]
    return make_plan(goal, raw, [{"type": "human", "spec": f"요청자가 결과가 목표를 충족함을 확인: {goal}"}], "heuristic", max_steps)


_OBJ = re.compile(r"\{.*\}", re.DOTALL)


async def gateway_plan(goal: str, ctx: TaskContext, max_steps: int, model: str) -> dict[str, Any]:
    base = os.environ["ATLAS_GATEWAY_URL"].rstrip("/")
    headers = otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}",
                           "x-atlas-tags": f"agent_id:al-plan,unit_id:AL-PLAN,tenant:{ctx.tenant}"})
    system = (await prompts.get("al-plan.system", fallback=SYSTEM_PROMPT)).render()   # 프롬프트 레지스트리 (BL-HARN-01)
    body = {"model": model, "response_format": {"type": "json_object"}, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": goal}]}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(f"{base}/v1/chat/completions", json=body, headers=headers)
    if res.status_code == 400 and res.json().get("error", {}).get("code") == "guard_blocked":
        raise PermissionError(f"guard_blocked: {res.json()['error'].get('message', '')}")   # 호출자(AL-RUN)가 POLICY_VIOLATION 으로 처리
    res.raise_for_status()
    content = res.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        # 보정: 약한 모델이 JSON 앞뒤에 군말을 붙이면 첫 객체만 떼어 읽는다 (BL-HARN-04 — 도입: mock-planner-sloppy 류, 제거 조건: ablation removable)
        m = _OBJ.search(content or "")
        if not (m and await flags.enabled("harness.plan_json_repair.enabled", default=True)):
            raise PlanInvalid(f"plan.invalid: JSON 아님 ({exc})") from exc
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as exc2:
            raise PlanInvalid(f"plan.invalid: JSON 복구 실패 ({exc2})") from exc2
    return make_plan(goal, parsed.get("steps") or [], parsed.get("dod") or [], f"gateway:{model}", max_steps)


def revise_plan(plan: dict[str, Any], digest: str) -> dict[str, Any]:
    """관찰 기반 재계획 (FR-AL-PLAN-05): 버전을 올리고, 남은 단계 앞에 '대안 시도' 단계를 넣는다."""
    steps = [dict(s) for s in plan.get("steps", [])]
    pending = [s for s in steps if s.get("status") != "done"]
    nid = f"s{len(steps) + 1}"
    alt = {"id": nid, "title": f"대안 시도: {digest[:120]}", "status": "pending",
           "depends_on": [s["id"] for s in steps if s.get("status") == "done"][-1:]}
    if pending:
        pending[0].setdefault("depends_on", []).append(nid)
    return {**plan, "steps": [*steps, alt], "version": int(plan.get("version", 1)) + 1, "revised_for": digest[:200]}


async def revise(ctx: TaskContext) -> dict[str, Any]:
    p, digest = ctx.data.get("plan"), ctx.data.get("observation_digest", "")
    if not isinstance(p, dict) or not digest:
        raise InputRequired("plan(객체)과 observation_digest 가 필요합니다")
    return revise_plan(p, digest)


async def plan(ctx: TaskContext) -> dict[str, Any]:
    goal = (ctx.data.get("goal") or ctx.text).strip()
    if not goal:
        raise InputRequired("계획할 목표(goal)를 text 또는 data.goal 로 보내세요")
    manifest = agent.manifest["spec"]
    max_steps = int(manifest["config"]["maxSteps"])
    if os.environ.get("ATLAS_GATEWAY_URL"):
        alias = manifest["harness"]["modelAlias"]
        if str(ctx.data.get("model_alias", "")).startswith("planner-"):   # 평가·ablation 용 플래너 alias 지정
            alias = ctx.data["model_alias"]
        try:
            return await gateway_plan(goal, ctx, max_steps, alias)
        except (PlanInvalid, httpx.HTTPError) as exc:
            # 보정: 모델 계획 실패 시 결정적 분해로 대체 (BL-HARN-04 — 제거 조건: 모델 계획 실패율 0)
            if await flags.enabled("harness.plan_heuristic_fallback.enabled", default=False):
                return {**heuristic_plan(goal, max_steps), "source": "heuristic-fallback", "fallback_reason": str(exc)[:200]}
            raise
    return heuristic_plan(goal, max_steps)


agent = AgentSpec.from_file(
    MANIFEST,
    description="AL-PLAN Planner — 목표를 단계 계획(TODO)과 검증 가능한 완료 정의(DoD)로 분해한다.",
    skills=[Skill(id="loop.plan", name="Plan", handler=plan,
                  description="목표 → Plan{id, goal, steps[], dod[]} (FR-AL-PLAN-01·02)",
                  examples=["사용자 로그인 기능을 설계하고 구현한 뒤 테스트한다"]),
            Skill(id="plan.revise", name="Revise", handler=revise,
                  description="관찰 요약으로 남은 계획을 수정 (FR-AL-PLAN-05)")],
)
