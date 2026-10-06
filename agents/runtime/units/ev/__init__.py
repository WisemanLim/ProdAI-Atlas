"""EV-GOLD Golden Datasets · EV-REG Regression Suites (FR-EV-GOLD-01·02·03·06 · EV-REG-01·02·03·04·06).

골든셋: POST /v1/datasets · POST /v1/datasets/{d}/items[:bulk|:from-trace] · PATCH|POST /v1/items/{id}[/transition] · POST /v1/datasets/{d}/versions
회귀:   POST /v1/suites/lint · POST /v1/runs (동기 실행 → gate pass|fail) · GET /v1/runs/{id}
승인(→approved)은 큐레이터(Bearer ATLAS_EV_CURATOR_TOKEN) 만 — PLT-IAM 역할 eval.curator 도입 전 대체.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import jsonschema
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext, otel
from units.ev import judging, runner
from units.ev import suite as suite_mod
from units.ev.store import APPROVED, DRAFT, IN_REVIEW, EvStore

MANIFEST = Path(__file__).with_name("atlas-agent.json")
MAX_BULK = 100_000
TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험에서 외부 HTTP(OB·GR·타깃) 대체


def http_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=TRANSPORT)


def store() -> EvStore:
    return EvStore(agent.resources["engine"])


def need_dataset(key: str):
    d = store().dataset(key)
    if d is None:
        raise HTTPException(404, "dataset not found")
    return d


def check_input(d, input_: Any) -> None:
    if d.input_schema:
        try:
            jsonschema.validate(input_, d.input_schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"input_schema 위반: {exc.message}") from exc


def item_view(i) -> dict[str, Any]:
    return {"id": i.id, "dataset_id": i.dataset_id, "input": i.input, "expected_output": i.expected_output, "rubric": i.rubric,
            "tags": i.tags, "status": i.status, "source": i.source, "source_trace_id": i.source_trace_id, "sha256": i.content_sha256}


router = APIRouter(prefix="/v1", tags=["evals"])


class DatasetIn(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,128}$")
    description: str = ""
    input_schema: dict[str, Any] | None = None


@router.post("/datasets", status_code=201)
async def create_dataset(body: DatasetIn):
    if store().dataset(body.name):
        raise HTTPException(409, "같은 이름의 데이터셋이 있음")
    d = store().create_dataset(body.name, body.description, body.input_schema)
    return {"id": d.id, "name": d.name}


class DatasetPatch(BaseModel):
    input_schema: dict[str, Any] | None


@router.patch("/datasets/{key}")
async def patch_dataset(key: str, body: DatasetPatch):
    """입력 스키마 지정·변경 — 활성(retired 제외) 항목이 모두 만족해야 한다."""
    d = need_dataset(key)
    if body.input_schema:
        bad = []
        for i in store().items(d.id):
            if i.status == "retired":
                continue
            try:
                jsonschema.validate(i.input, body.input_schema)
            except jsonschema.ValidationError:
                bad.append(i.id)
        if bad:
            raise HTTPException(409, {"message": "스키마에 맞지 않는 활성 항목 — retire 후 다시 시도", "items": bad})
    store().set_schema(d.id, body.input_schema)
    return {"name": d.name, "input_schema": body.input_schema}


@router.get("/datasets")
async def list_datasets():
    return {"items": [{"id": d.id, "name": d.name, "description": d.description,
                       "counts": {s: len(store().items(d.id, s)) for s in (DRAFT, IN_REVIEW, APPROVED)}} for d in store().datasets()]}


class ItemIn(BaseModel):
    input: Any
    expected_output: Any = None
    rubric: Any = None
    tags: list[str] = []


@router.post("/datasets/{key}/items", status_code=201)
async def add_item(key: str, body: ItemIn):
    d = need_dataset(key)
    try:
        check_input(d, body.input)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return item_view(store().add_item(d.id, body.input, body.expected_output, body.rubric, body.tags, "manual"))


@router.post("/datasets/{key}/items:bulk")
async def bulk(key: str, request: Request):
    """JSONL 일괄 업로드 — 줄 단위 스트리밍 파싱, 최대 10만 행. 줄 번호별 거부 사유 반환."""
    d = need_dataset(key)
    accepted, rejected, buf, line_no = 0, [], b"", 0

    def handle(raw: bytes) -> None:
        nonlocal accepted, line_no
        line_no += 1
        if not raw.strip():
            return
        if line_no > MAX_BULK:
            raise HTTPException(413, f"최대 {MAX_BULK} 행")
        try:
            obj = ItemIn(**json.loads(raw))
            check_input(d, obj.input)
        except Exception as exc:  # noqa: BLE001 — 줄별 오류는 보고만
            rejected.append({"line": line_no, "error": str(exc)[:200]})
            return
        store().add_item(d.id, obj.input, obj.expected_output, obj.rubric, obj.tags, "bulk")
        accepted += 1

    async for chunk in request.stream():
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            handle(ln)
    if buf:
        handle(buf)
    return {"accepted": accepted, "rejected": rejected}


class FromTrace(BaseModel):
    trace_id: str
    expected_output: Any = None
    tags: list[str] = ["prod-trace"]


async def mask(texts: list[str], tenant: str) -> list[str]:
    """승격 시 PII 마스킹 재적용 (GR-IO retrieval 경계, 없으면 그대로)."""
    url = os.environ.get("ATLAS_GUARD_URL")
    if not url:
        return texts
    async with http_client(10) as c:
        res = await c.post(url.rstrip("/") + "/v1/guard/check", json={"tenant": tenant, "boundary": "retrieval", "contents": texts})
    res.raise_for_status()
    return res.json().get("contents", texts)


@router.post("/datasets/{key}/items:from-trace", status_code=201)
async def from_trace(key: str, body: FromTrace, tenant: str = "default"):
    """실패 trace → 골든셋 항목 초안 (FR-EV-GOLD-03): 입력·출력 추출 → 마스킹 재적용 → 검수 대기(in_review)."""
    d = need_dataset(key)
    ob = os.environ.get("ATLAS_OB_URL", "http://agent-ob:8000").rstrip("/")
    async with http_client(15) as c:
        res = await c.get(f"{ob}/v1/traces/{body.trace_id}/io", headers=otel.inject({}))
    if res.status_code == 404:
        raise HTTPException(404, "trace not found")
    res.raise_for_status()
    io = res.json()
    if not io.get("captured") or not io.get("input"):
        raise HTTPException(409, "trace 에 입력 본문이 없음 (ob.capture=metadata/none)")
    inp, out = await mask([io["input"], io.get("output") or ""], tenant)
    try:                                                     # 승격 항목도 데이터셋 입력 스키마를 지킨다 (형식 혼입 방지)
        check_input(d, {"prompt": inp})
    except ValueError as exc:
        raise HTTPException(422, f"{exc} — trace 승격 입력은 {{'prompt': …}} 형식") from exc
    expected = body.expected_output if body.expected_output is not None else {"observed_output": out}
    item = store().add_item(d.id, {"prompt": inp}, expected, None, body.tags, "prod-trace", trace_id=body.trace_id, status=IN_REVIEW)
    return item_view(item)


@router.get("/datasets/{key}/items")
async def list_items(key: str, status: str | None = None):
    return {"items": [item_view(i) for i in store().items(need_dataset(key).id, status)]}


class ItemPatch(BaseModel):
    input: Any = None
    expected_output: Any = None
    rubric: Any = None
    tags: list[str] | None = None


@router.patch("/items/{iid}")
async def patch_item(iid: str, body: ItemPatch):
    i = store().item(iid)
    if i is None:
        raise HTTPException(404, "item not found")
    if i.status not in (DRAFT, IN_REVIEW):
        raise HTTPException(409, f"{i.status} 항목은 수정 불가 — retire 후 새 항목 (버전 불변성)")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "input" in fields:
        try:
            check_input(need_dataset(i.dataset_id), fields["input"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return item_view(store().update_item(iid, **fields))


class Transition(BaseModel):
    to: str = Field(pattern="^(draft|in_review|approved|retired)$")
    verdict: str | None = Field(None, pattern="^(pass|fail)$")
    critique: str | None = None


@router.post("/items/{iid}/transition")
async def transition(iid: str, body: Transition, authorization: str | None = Header(None)):
    """검수 상태머신 draft→in_review→approved→retired (FR-EV-GOLD-06). 승인은 큐레이터만, 라벨 이력 append-only."""
    if store().item(iid) is None:
        raise HTTPException(404, "item not found")
    actor = "anonymous"
    token = os.environ.get("ATLAS_EV_CURATOR_TOKEN", "")
    if token and token != "CHANGE_ME" and authorization == f"Bearer {token}":
        actor = "curator"
    if body.to == APPROVED and actor != "curator":
        raise HTTPException(403, "승인은 eval.curator 만")
    try:
        i = store().transition(iid, body.to, actor, body.verdict, body.critique)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {**item_view(i), "labels": [{"from": lb.from_status, "to": lb.to_status, "verdict": lb.verdict, "critique": lb.critique,
                                        "actor": lb.actor} for lb in store().labels(iid)]}


@router.post("/datasets/{key}/versions", status_code=201)
async def snapshot(key: str):
    """승인 항목의 불변 버전 스냅샷 (FR-EV-GOLD-02) — 실험은 (데이터셋, 버전) 만 참조."""
    d = need_dataset(key)
    v = store().snapshot(d.id)
    return {"dataset": d.name, "version": v.version, "items": len(v.items), "manifest_sha256": v.manifest_sha256}


@router.get("/datasets/{key}/versions/{version}")
async def get_version(key: str, version: int):
    v = store().version(need_dataset(key).id, version)
    if v is None:
        raise HTTPException(404, "version not found")
    return {"version": v.version, "manifest_sha256": v.manifest_sha256, "items": v.items}


@router.post("/suites/lint")
async def lint(request: Request):
    try:
        s = suite_mod.load((await request.body()).decode())
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "name": s["name"], "targets": [t["id"] for t in s["targets"]], "epochs": s["epochs"]}


async def run_suite(suite_text: str | dict[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    transport = transport or TRANSPORT
    s = suite_mod.load(suite_text)
    d = store().dataset(s["dataset"]["name"])
    if d is None:
        raise HTTPException(404, f"dataset {s['dataset']['name']} 없음")
    v = store().version(d.id, s["dataset"].get("version"))
    if v is None or not v.items:
        raise HTTPException(409, "승인 항목 스냅샷(버전) 없음 — POST /v1/datasets/{d}/versions")
    judges, reasons_pre = {}, []
    for sc in s["scorers"]:
        if sc["type"] == "judge":
            j = store().judge(sc["judge"], sc.get("version"))
            if j is None:
                raise HTTPException(404, f"judge {sc['judge']} 없음")
            judges[sc["judge"]] = j
            cal = j.calibration or {}
            if not cal.get("trusted") and not sc.get("allow_uncalibrated"):   # 사람 일치도 미달 judge 는 게이트에 못 쓴다 (EV-JUDGE-05)
                reasons_pre.append(f"judge {j.name}@v{j.version} 미보정(κ={cal.get('kappa')}) — POST /v1/judges/{j.name}/calibrate")
    run = store().create_run(s, d.id, v.version)
    try:
        results, summary = await runner.execute(s, v.items, run.id, transport, judges)
    except Exception as exc:
        store().finish_run(run.id, "error", None, {"error": str(exc)[:300]})
        raise
    baseline = None
    if s.get("baseline_run_id"):
        b = store().run(s["baseline_run_id"])
        baseline = b.summary if b and b.summary else None
    g, reasons = runner.gate(s, summary, baseline)
    if reasons_pre:
        g, reasons = "fail", reasons_pre + reasons
    summary["gate_reasons"] = reasons
    store().add_results(results)
    by_item = {i["item_id"]: i for i in v.items}
    store().add_scores([{"source": "offline", "run_id": run.id, "item_id": r["item_id"], "target": r["target"],
                         "scorer": f"judge:{sc['judge']}" if sc["type"] == "judge" else sc["type"], "judge_version": sc.get("version"),
                         "value": float(sc.get("score", 1.0 if sc["passed"] else 0.0)), "passed": sc["passed"], "critique": sc.get("critique"),
                         "payload": {"input": by_item[r["item_id"]].get("input"), "expected": by_item[r["item_id"]].get("expected_output"),
                                     "output": r["output"]} if sc["type"] == "judge" else None}
                        for r in results for sc in r["scores"]])
    store().finish_run(run.id, "done", g, summary)
    return {"run_id": run.id, "suite": s["name"], "dataset": d.name, "dataset_version": v.version, "gate": g, "summary": summary}


@router.post("/runs")
async def create_run(request: Request):
    body = (await request.body()).decode()
    try:
        return await run_suite(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/runs")
async def list_runs(limit: int = 50):
    return {"items": [{"run_id": r.id, "suite": r.suite_name, "status": r.status, "gate": r.gate, "dataset_version": r.dataset_version,
                       "pass_rate": (r.summary or {}).get("pass_rate"), "created_at": r.created_at.isoformat()}
                      for r in store().runs(min(limit, 200))]}


@router.get("/runs/{rid}")
async def get_run(rid: str, results: bool = False):
    r = store().run(rid)
    if r is None:
        raise HTTPException(404, "run not found")
    out = {"run_id": r.id, "suite": r.suite_name, "status": r.status, "gate": r.gate, "dataset_version": r.dataset_version, "summary": r.summary}
    if results:
        out["results"] = [{"item_id": x.item_id, "target": x.target, "epoch": x.epoch, "passed": x.passed, "scores": x.scores,
                           "output": x.output, "error": x.error} for x in store().results(rid)]
    return out


async def run_skill(ctx: TaskContext) -> dict[str, Any]:
    if "suite" not in ctx.data:
        raise InputRequired("data.suite(YAML 문자열 또는 객체) 가 필요합니다")
    return await run_suite(ctx.data["suite"])


agent = AgentSpec.from_file(MANIFEST, description="EV-REG·EV-GOLD — 골든셋(검수·불변 버전·trace 승격)과 회귀 스위트(pass^k·CI 게이트).",
                            skills=[Skill(id="ev.regression", name="Run suite", handler=run_skill, description="eval-as-code 스위트 실행 → gate")],
                            routers=[router, judging.router], background=[judging.online_loop])
