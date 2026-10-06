"""MAT Maturity Assessor — Reliable·Observable·Cost-aware·Safe 4속성 점수, L0~L4 단계·게이트, 9개념 커버리지, 복리 루프, 90일 로드맵,
전사 지표, 월간 스냅샷·리포트, 홈 진화 띠, 성과 지표 (FR-MAT-01~06·08~10).

  GET /v1/assessment · /v1/band · /v1/coverage · /v1/roadmap[?format=markdown|github] · /v1/metrics · /v1/definition
  POST·GET /v1/snapshots · GET /v1/report[?format=markdown]
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from atlas_agent_core import AgentSpec, Skill, TaskContext, events
from units.mat import engine
from units.mat.collect import Collector
from units.mat.store import MatStore

MANIFEST = Path(__file__).with_name("atlas-agent.json")
DEFINITION = yaml.safe_load(Path(__file__).with_name("maturity.yaml").read_text(encoding="utf-8"))
log = logging.getLogger("atlas.mat")
router = APIRouter(prefix="/v1", tags=["maturity"])
_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}


def store() -> MatStore:
    return MatStore(agent.resources["engine"])


async def assess(org: str = "default", project: str = "default", fresh: bool = False) -> dict[str, Any]:
    key = (org, project)
    hit = _cache.get(key)
    if hit and not fresh and time.monotonic() - hit[1] < float(agent.manifest["spec"]["config"]["cacheS"]):
        return hit[0]
    col = Collector(agent, org)
    facts = await col.facts()
    hist = [MatStore.view(r) for r in store().history(org, project)]
    out = {"org": org, "project": project, "assessed_at": datetime.now(UTC).isoformat(), **engine.assess(DEFINITION, facts, hist),
           "facts": facts, "source_errors": col.errors}
    _cache[key] = (out, time.monotonic())
    return out


@router.get("/assessment")
async def assessment(org: str = "default", project: str = "default", fresh: bool = False):
    return await assess(org, project, fresh)


@router.get("/band")
async def band(org: str = "default", project: str = "default"):
    a = await assess(org, project)
    return {**a["band"], "assessed_at": a["assessed_at"]}


@router.get("/coverage")
async def coverage(org: str = "default", project: str = "default"):
    return (await assess(org, project))["coverage"]


@router.get("/metrics")
async def metrics(org: str = "default", project: str = "default"):
    return {"items": (await assess(org, project))["metrics"]}


@router.get("/definition")
async def definition():
    return DEFINITION


def roadmap_md(r: dict[str, Any], a: dict[str, Any]) -> str:
    lines = [f"# 90일 성숙도 로드맵 — {a['org']}/{a['project']}", "", f"현재 L{a['stage']['level']} {a['stage']['name']} → 다음 {a['band']['next'] or '없음'}", "",
             "| 주차 | 단계 | 작업 | 역할 | 게이트 |", "|---|---|---|---|---|"]
    lines += [f"| {c['weeks'][0]}~{c['weeks'][1]} | L{c['level']} | {c['title']} | {c['role']} | `{c['gate']}` |" for c in r["cards"]]
    return "\n".join(lines) + "\n"


@router.get("/roadmap")
async def roadmap(org: str = "default", project: str = "default", format: str = "json"):
    a = await assess(org, project)
    r = a["roadmap"]
    if format == "markdown":
        return PlainTextResponse(roadmap_md(r, a), media_type="text/markdown; charset=utf-8")
    if format == "github":
        return {"issues": r["github_issues"]}
    return r


async def snapshot(org: str, project: str, kind: str) -> dict[str, Any]:
    prev = store().history(org, project)
    a = await assess(org, project, fresh=True)
    scores = {k: v["score"] for k, v in a["attributes"].items()}
    row = store().add(org, project, a["stage"]["level"], scores, a["facts"], kind)
    if prev and prev[-1].level != row.level:
        await events.publish(agent, "plt.mat.stage_changed", {"project": project, "from": prev[-1].level, "to": row.level, "name": a["stage"]["name"]}, org)
    return MatStore.view(row)


@router.post("/snapshots", status_code=201)
async def create_snapshot(org: str = "default", project: str = "default"):
    return await snapshot(org, project, "manual")


@router.get("/snapshots")
async def list_snapshots(org: str = "default", project: str = "default"):
    return {"items": [MatStore.view(r) for r in store().history(org, project)]}


@router.get("/report")
async def report(org: str = "default", project: str = "default", format: str = "markdown"):
    """경영진용 진단 리포트 (FR-MAT-08) — 현재 판정 · 이력 · 조직 롤업(프로젝트별 단계 분포)."""
    a = await assess(org, project)
    hist = store().history(org, project)
    roll: dict[int, list[str]] = {}
    for r in store().all_latest():
        if r.org == org:
            roll.setdefault(r.level, []).append(r.project)
    if format == "json":
        return {"assessment": a, "history": [MatStore.view(r) for r in hist], "rollup": roll}
    oc = a["outcomes"]["cards"]
    lines = [f"# 성숙도 진단 리포트 — {org}/{project}", "", f"- 진단 시각: {a['assessed_at']}",
             f"- 단계: **L{a['stage']['level']} {a['stage']['name']}** ({a['stage']['phase']}) · 다음: {a['band']['next'] or '-'}",
             f"- 다음 단계 미달 게이트: {', '.join(a['band']['unmet_gates']) or '없음'}", "", "## 4속성 점수", "", "| 속성 | 점수 |", "|---|---|"]
    lines += [f"| {engine.ATTR_LABEL[k]} | {v['score']} |" for k, v in a["attributes"].items()]
    lines += ["", "## 성과 (기준선 대비)", "", "| 지표 | 현재 | 기준선 | 변화 |", "|---|---|---|---|"]
    lines += [f"| {c['name']} ({c['fact']}) | {c['current']} | {c['baseline']} | {c['change_pct'] if c['change_pct'] is not None else '-'}% |" for c in oc]
    cp = a["compound"]
    lines += ["", "## 복리 루프", "", f"- trace 승격(30일): {cp['trace_promotions_30d']} · 회귀 포함률: {cp['promotions_in_regression_rate']}",
              f"- 회귀로 차단된 변경(30일): {cp['changes_blocked_by_regression_30d']} · 모델 교체 회귀: {cp['model_swap_regressions']} · 비용 추세: {cp['cost_per_task_trend']}",
              "", "## 이력", "", "| 시각 | 단계 | Reliable | Observable | Cost-aware | Safe |", "|---|---|---|---|---|---|"]
    lines += [f"| {r.created_at.isoformat()[:19]} | L{r.level} | {r.scores.get('reliable')} | {r.scores.get('observable')} | {r.scores.get('cost_aware')} | {r.scores.get('safe')} |" for r in hist]
    lines += ["", "## 조직 롤업 (프로젝트별 최신 단계)", ""] + [f"- L{lv}: {', '.join(sorted(ps))}" for lv, ps in sorted(roll.items())]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/markdown; charset=utf-8")


async def monthly_loop() -> None:
    """월 1회 스냅샷 (FR-MAT-08) — 이번 달 스냅샷이 없는 (org, project) 에 만든다. 주기적으로 확인."""
    every = float(agent.manifest["spec"]["config"]["snapshotCheckS"])
    while True:
        await asyncio.sleep(every)
        month = datetime.now(UTC).strftime("%Y-%m")
        keys = {(r.org, r.project) for r in store().all_latest()} or {("default", "default")}
        for org, project in keys:
            if not any(r.created_at.strftime("%Y-%m") == month for r in store().history(org, project)):
                try:
                    await snapshot(org, project, "monthly")
                except Exception as exc:  # noqa: BLE001
                    log.warning("monthly snapshot %s/%s: %s", org, project, exc)


async def assess_skill(ctx: TaskContext) -> dict[str, Any]:
    a = await assess(ctx.data.get("org") or ctx.tenant, ctx.data.get("project", "default"))
    return {k: a[k] for k in ("band", "stage", "attributes", "coverage", "compound", "roadmap")}


agent = AgentSpec.from_file(MANIFEST, description="MAT 성숙도 진단 — 4속성 점수·L0~L4 단계·9개념 커버리지·복리 루프·90일 로드맵·월간 리포트",
                            skills=[Skill(id="mat.assess", name="Assess maturity", handler=assess_skill,
                                          description="플랫폼 텔레메트리로 성숙도 단계·미달 게이트·로드맵 산출 (FR-MAT-01·02·05)", tags=["maturity"])],
                            routers=[router], background=[monthly_loop])
agent.resources["mat_def"] = DEFINITION
