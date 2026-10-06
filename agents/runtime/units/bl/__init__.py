"""BL Bitter Lesson — 모델 교체 자동화(BL-MODEL) · 하네스 외부화(BL-HARN) · 계산 정책(BL-COMP).

  모델:  GET /v1/models · /v1/models/eol · POST /v1/models/eol/scan · GET /v1/models/{m}/card · GET /v1/aliases[/{a}/history] · POST /v1/aliases/{a}/rollback
  후보:  POST /v1/models/{m}/candidate · GET /v1/candidates[/{id}] · POST /v1/candidates/{id}/evaluate · /canary · /canary/advance · /promote · /rollback
  하네스: POST·GET /v1/{prompts|harness|rules}/{name} · POST …/{name}/labels/{label} (production = 게시 게이트) · POST /v1/rules/{name}/evaluate
  플래그: GET /v1/flags · PUT /v1/flags/{key} · POST /v1/ablations
  계산:  POST /v1/compute/best-of-n · POST /v1/compute/curve · GET /v1/audit
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext, events
from units.bl import clients, harness, models
from units.bl.expr import ExprError
from units.bl.store import BlStore

MANIFEST = Path(__file__).with_name("atlas-agent.json")
log = logging.getLogger("atlas.bl")
router = APIRouter(prefix="/v1", tags=["bitter-lesson"])
arts = APIRouter(prefix="/v1", tags=["harness"])   # /{prompts|harness|rules}/… — 구체 경로 뒤에 등록해 가로채지 않게


def store() -> BlStore:
    return BlStore(agent.resources["engine"])


async def publish(subject: str, data: dict[str, Any]) -> None:
    await events.publish(agent, subject, data)


def life(actor: str = "bl") -> models.Lifecycle:
    return models.Lifecycle(store(), publish, actor)


def bad(exc: Exception) -> HTTPException:
    if isinstance(exc, clients.GatewayError):
        return HTTPException(502 if exc.status >= 500 else 400, str(exc))
    return HTTPException(422, str(exc))


def cand_view(c) -> dict[str, Any]:
    return {"id": c.id, "alias": c.alias, "base_model": c.base_model, "model": c.cand_model, "status": c.status, "decision": c.decision,
            "alias_version_before": c.alias_version_before, "detail": c.detail, "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat()}


def need_cand(cid: str):
    c = store().candidate(cid)
    if c is None:
        raise HTTPException(404, "candidate not found")
    return c


# ---- 모델·alias (BL-MODEL-01·02·07·08) ----

@router.get("/models")
async def list_models():
    cat, al = await clients.catalog(), await clients.aliases()
    for m in cat:
        m["serving_aliases"] = [a["name"] for a in al if any(d["model"] == m["model"] and d.get("weight", 0) > 0 for d in a["deployments"])]
    return {"items": cat}


@router.get("/models/eol")
async def eol(days: int | None = None):
    days = days or int(agent.manifest["spec"]["config"]["eolWarnDays"])
    cat, al = await clients.catalog(), await clients.aliases()
    out = []
    for m in cat:
        if m.get("deprecation_in") is not None and m["deprecation_in"] <= days:
            using = [a["name"] for a in al if any(d["model"] == m["model"] and d.get("weight", 0) > 0 for d in a["deployments"])]
            rep = models.replacement(cat, m, days)
            out.append({"model": m["model"], "deprecation_date": m.get("deprecation_date"), "days_left": m["deprecation_in"], "aliases": using,
                        "replacement": rep["model"] if rep else None})
    return {"days": days, "items": out}


class EolScan(BaseModel):
    days: int | None = None
    suites: list[dict[str, Any]] = []
    evaluate: bool = False


@router.post("/models/eol/scan")
async def eol_scan(body: EolScan):
    """수명종료 대응 (FR-BL-MODEL-07): 폐기 예정 모델을 서빙 중인 alias 마다 경고 이벤트 + 대체 후보 자동 등록(이미 진행 중이면 건너뜀)."""
    rep = await eol(body.days)
    active = {(c.alias, c.cand_model) for c in store().candidates() if c.status in ("registered", "evaluating", "decided", "canary")}
    registered = []
    for it in rep["items"]:
        await publish("plt.bl.model.eol_warning", {"model": it["model"], "days_left": it["days_left"], "aliases": it["aliases"], "replacement": it["replacement"]})
        for a in it["aliases"]:
            if not it["replacement"] or (a, it["replacement"]) in active:
                continue
            c = await life().register(it["replacement"], a, body.suites, note=f"EOL 대체: {it['model']} ({it['days_left']}일 남음)", by="bl-eol")
            if body.evaluate and body.suites:
                c = await life().evaluate(c.id)
            registered.append(cand_view(c))
    return {"warnings": rep["items"], "registered": registered}


@router.get("/models/{model}/card")
async def card(model: str, format: str = "markdown"):
    cat = {m["model"]: m for m in await clients.catalog()}
    if model not in cat:
        raise HTTPException(404, "model not in catalog")
    md = models.model_card(model, cat, await clients.aliases(), store().candidates(),
                           [a for a in store().audits() if a.detail.get("from") == model or a.detail.get("to") == model or a.detail.get("model") == model])
    if format == "json":
        return {"model": model, "markdown": md}
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")


@router.get("/aliases")
async def list_aliases():
    return {"items": await clients.aliases()}


@router.get("/aliases/{name}/history")
async def alias_history(name: str):
    return {"items": (await clients.gw("GET", f"/admin/aliases/{name}/history")).get("data", [])}


class Rollback(BaseModel):
    version: int
    by: str = "operator"


@router.post("/aliases/{name}/rollback")
async def alias_rollback(name: str, body: Rollback):
    try:
        out = await clients.rollback_alias(name, body.version, body.by)
    except clients.GatewayError as exc:
        raise bad(exc) from exc
    store().audit(body.by, "alias.rollback", f"alias:{name}", {"to_version": body.version, "new_version": out["version"]})
    await publish("plt.bl.alias.rolled_back", {"alias": name, "to_version": body.version, "by": body.by})
    return out


# ---- 후보 (BL-MODEL-03~06) ----

class CandidateIn(BaseModel):
    alias: str
    suites: list[dict[str, Any]] = Field(min_length=1)
    k: int = Field(4, ge=1, le=10)
    rules: list[dict[str, str]] | None = None
    evaluate: bool = True
    by: str = "operator"


@router.post("/models/{model}/candidate", status_code=201)
async def register(model: str, body: CandidateIn):
    try:
        c = await life().register(model, body.alias, body.suites, body.k, body.rules, by=body.by)
        if body.evaluate:
            c = await life().evaluate(c.id)
    except (ValueError, ExprError, clients.GatewayError, RuntimeError) as exc:
        raise bad(exc) from exc
    return cand_view(c)


@router.get("/candidates")
async def list_candidates():
    return {"items": [cand_view(c) for c in store().candidates()]}


@router.get("/candidates/{cid}")
async def get_candidate(cid: str):
    return cand_view(need_cand(cid))


@router.post("/candidates/{cid}/evaluate")
async def evaluate(cid: str):
    need_cand(cid)
    try:
        return cand_view(await life().evaluate(cid))
    except (ValueError, RuntimeError, clients.GatewayError) as exc:
        raise bad(exc) from exc


class CanaryIn(BaseModel):
    stages: list[int] | None = None
    min_samples: int = Field(50, ge=1)
    slo: dict[str, float] | None = None
    force: bool = False
    auto: bool = False


@router.post("/candidates/{cid}/canary")
async def canary(cid: str, body: CanaryIn):
    need_cand(cid)
    try:
        c = await life().canary_start(cid, body.stages, body.min_samples, body.slo, body.force)
    except (ValueError, clients.GatewayError) as exc:
        raise bad(exc) from exc
    if body.auto:
        store().update_candidate(cid, detail={**c.detail, "canary": {**c.detail["canary"], "auto": True}})
    return cand_view(store().candidate(cid))


@router.post("/candidates/{cid}/canary/advance")
async def canary_advance(cid: str):
    need_cand(cid)
    try:
        return await life().canary_advance(cid)
    except (ValueError, clients.GatewayError) as exc:
        raise bad(exc) from exc


class Actor(BaseModel):
    by: str = "operator"
    reason: str = "manual"


@router.post("/candidates/{cid}/promote")
async def promote(cid: str, body: Actor):
    c = need_cand(cid)
    if c.status not in ("decided", "canary"):
        raise HTTPException(409, f"승격 불가 상태 {c.status}")
    return cand_view(await life(body.by).promote(cid, by=body.by))


@router.post("/candidates/{cid}/rollback")
async def rollback(cid: str, body: Actor):
    c = need_cand(cid)
    if c.status in ("rolled_back",):
        raise HTTPException(409, "이미 롤백됨")
    return cand_view(await life(body.by).rollback(cid, reason=body.reason, by=body.by))


async def canary_loop() -> None:
    """auto 카나리를 주기적으로 진행한다 (표본 부족이면 대기). 기동 시 flagd 파일을 DB 정본으로 다시 쓴다."""
    harness.write_flagd(store().flags())
    every = float(agent.manifest["spec"]["config"]["canaryIntervalS"])
    while True:
        await asyncio.sleep(every)
        for c in store().candidates():
            if c.status == "canary" and (c.detail.get("canary") or {}).get("auto"):
                try:
                    await life("bl-auto").canary_advance(c.id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("canary %s: %s", c.id, exc)


# ---- 하네스 아티팩트 (BL-HARN-01·02·03·06) ----

def art_view(kind: str, r) -> dict[str, Any]:
    return {"kind": kind, "name": r.name, "version": r.version, **r.body, "created_by": r.created_by, "created_at": r.created_at.isoformat(),
            "labels": [k for k, v in store().labels(kind, r.name).items() if v == r.version]}


def kind_of(seg: str) -> str:
    k = {"prompts": "prompt", "harness": "harness", "rules": "rules"}.get(seg)
    if not k:
        raise HTTPException(404, "unknown kind")
    return k


@arts.post("/{seg}/{name}", status_code=201)
async def create_artifact(seg: str, name: str, body: dict[str, Any]):
    kind = kind_of(seg)
    try:
        clean = harness.validate_body(kind, body)
    except (ValueError, ExprError) as exc:
        raise bad(exc) from exc
    r = store().add_artifact(kind, name, clean, str(body.get("by", "operator")))
    store().audit(r.created_by, f"{kind}.create", f"{kind}:{name}", {"version": r.version})
    if not store().labels(kind, name):            # 첫 버전은 staging 라벨
        store().set_label(kind, name, "staging", r.version)
    return art_view(kind, r)


@arts.get("/{seg}")
async def list_artifacts(seg: str):
    kind = kind_of(seg)
    return {"items": [art_view(kind, r) for r in store().artifacts(kind)]}


@arts.get("/{seg}/{name}")
async def get_artifact(seg: str, name: str, label: str | None = None, version: int | None = None):
    kind = kind_of(seg)
    r = store().artifact(kind, name, version, None if version else (label or None))
    if r is None:
        raise HTTPException(404, f"{kind} {name} 없음" + (f" (label {label})" if label else ""))
    return art_view(kind, r)


class LabelMove(BaseModel):
    version: int
    gate_suite: dict[str, Any] | None = None
    override: bool = False
    reason: str = ""
    by: str = "operator"


@arts.post("/{seg}/{name}/labels/{label}")
async def move_label(seg: str, name: str, label: str, body: LabelMove, authorization: str | None = Header(None)):
    """라벨 이동. production 은 게시 게이트 (FR-BL-HARN-06): 후보 버전을 고정한 회귀 스위트 통과 시에만. 긴급 우회 = override + 관리 토큰 + 감사."""
    kind = kind_of(seg)
    if store().artifact(kind, name, body.version) is None:
        raise HTTPException(404, "version not found")
    gate = None
    if label == "production":
        if body.override:
            if authorization != f"Bearer {await clients.admin_token()}" or not body.reason:
                raise HTTPException(403, "override 는 플랫폼 관리 토큰 + reason 필요")
            store().audit(body.by, f"{kind}.publish.override", f"{kind}:{name}", {"version": body.version, "reason": body.reason})
        else:
            if not body.gate_suite:
                raise HTTPException(422, "production 게시는 gate_suite 필요 (FR-BL-HARN-06)")
            pin = {"atlas.prompts" if kind == "prompt" else "atlas.harness": {name: body.version}}
            try:
                run = await clients.ev_run(harness.with_metadata(body.gate_suite, pin, f"gate-{name}-v{body.version}"))
            except RuntimeError as exc:
                raise bad(exc) from exc
            gate = {"run_id": run["run_id"], "gate": run["gate"], "pass_rate": run["summary"]["pass_rate"], "reasons": run["summary"].get("gate_reasons")}
            if run["gate"] != "pass":
                store().audit(body.by, f"{kind}.publish.blocked", f"{kind}:{name}", {"version": body.version, **gate})
                raise HTTPException(409, {"message": "게시 게이트 실패 — 라벨 미적용", **gate, "diff": f"/v1/runs/{run['run_id']}?results=true"})
    store().set_label(kind, name, label, body.version)
    store().audit(body.by, f"{kind}.label", f"{kind}:{name}", {"label": label, "version": body.version, "gate": gate})
    if label == "production":
        await publish(f"plt.bl.{kind}.published", {"name": name, "version": body.version, "gate": gate, "override": body.override})
    return {"kind": kind, "name": name, "label": label, "version": body.version, "gate": gate}


@router.post("/rules/{name}/evaluate")
async def eval_rules(name: str, body: dict[str, Any], label: str = "production", version: int | None = None):
    r = store().artifact("rules", name, version, None if version else label) or store().artifact("rules", name)
    if r is None:
        raise HTTPException(404, "rules not found")
    return {"name": name, "version": r.version, **harness.evaluate_rules(r.body["rules"], body.get("facts") or {})}


# ---- 보정 플래그 (BL-HARN-04·05) ----

class FlagIn(BaseModel):
    enabled: bool
    reason: str = Field(min_length=3)
    introduced_for: str = Field(min_length=1)          # 도입 모델(버전)
    removal_condition: str = Field(min_length=3)
    by: str = "operator"


def flag_view(f) -> dict[str, Any]:
    return {"key": f.key, "enabled": f.enabled, "reason": f.reason, "introduced_for": f.introduced_for, "removal_condition": f.removal_condition,
            "updated_at": f.updated_at.isoformat()}


@router.get("/flags")
async def list_flags():
    return {"items": [flag_view(f) for f in store().flags()], "file": str(harness.flags_path())}


@router.put("/flags/{key}")
async def put_flag(key: str, body: FlagIn):
    if not key.startswith("harness.") or not key.endswith(".enabled"):
        raise HTTPException(422, "키 형식 harness.<component>.enabled")
    f = store().put_flag(key=key, enabled=body.enabled, reason=body.reason, introduced_for=body.introduced_for, removal_condition=body.removal_condition)
    harness.write_flagd(store().flags())
    store().audit(body.by, "flag.put", f"flag:{key}", {"enabled": body.enabled})
    return flag_view(f)


class AblationIn(BaseModel):
    suite: dict[str, Any]
    flags: list[str] = Field(min_length=1)


@router.post("/ablations")
async def ablate(body: AblationIn):
    try:
        out = await harness.ablation(body.suite, body.flags)
    except (ValueError, RuntimeError) as exc:
        raise bad(exc) from exc
    store().audit("bl", "ablation", "flags:" + ",".join(body.flags), {"removable": out["removable"]})
    return out


# ---- 계산 (BL-COMP-03·06) ----

class BestOfN(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    n: int = 4
    verifier: dict[str, Any]
    max_usd: float = Field(0.05, gt=0)
    temperature: float = Field(0.8, gt=0, le=2)


@router.post("/compute/best-of-n")
async def bon(body: BestOfN):
    try:
        return await harness.best_of_n(body.model, body.messages, body.n, body.verifier, body.max_usd, body.temperature)
    except ValueError as exc:
        raise bad(exc) from exc


class CurveIn(BaseModel):
    suite: dict[str, Any]
    efforts: list[str] = ["low", "medium", "high"]
    tolerance_pp: float = 1.0


@router.post("/compute/curve")
async def compute_curve(body: CurveIn):
    try:
        return await harness.curve(body.suite, body.efforts, body.tolerance_pp)
    except (ValueError, RuntimeError) as exc:
        raise bad(exc) from exc


@router.get("/audit")
async def audit(target: str | None = None, limit: int = 100):
    return {"items": [{"ts": a.ts.isoformat(), "actor": a.actor, "action": a.action, "target": a.target, "detail": a.detail}
                      for a in store().audits(target, min(limit, 500))]}


# ---- A2A skill ----

async def validate_rule(ctx: TaskContext) -> dict[str, Any]:
    name = ctx.data.get("rules")
    if not name:
        raise InputRequired("data.rules(규칙 세트 이름)·facts 가 필요합니다")
    return await eval_rules(name, {"facts": ctx.data.get("facts") or {}})


async def best_of_n_skill(ctx: TaskContext) -> dict[str, Any]:
    try:
        return await bon(BestOfN(**ctx.data))
    except Exception as exc:
        raise ValueError(str(exc)) from exc


agent = AgentSpec.from_file(MANIFEST, description="BL Bitter Lesson — 모델 교체 자동화(후보·평가·카나리·전환), 하네스 외부화(프롬프트·하네스·규칙·보정 플래그), 계산 정책(best-of-n·품질-계산 곡선)",
                            skills=[Skill(id="bl.validate_rule", name="Validate rule", handler=validate_rule,
                                          description="도메인 규칙 세트(YAML·버전)로 사실(facts)을 평가 → allow·require_approval·deny (BL-HARN-02)", tags=["rules", "policy"]),
                                    Skill(id="bl.best_of_n", name="Best of N", handler=best_of_n_skill,
                                          description="검증기가 있는 과제를 N 개 샘플로 풀고 첫 통과 답을 고른다 (BL-COMP-03)", tags=["compute"])],
                            routers=[router, arts], background=[canary_loop])
