"""OB-TRACE·OB-DEBUG·OB-DASH 관측 서비스 — ClickHouse(otel_traces) 조회 API + SLO·경보 (FR-OB-TRACE-03·07, OB-DEBUG-01, OB-DASH-01·03·04·05).

GET  /v1/traces?q=&since=&limit=        trace 검색 (필터 DSL)
GET  /v1/traces/{id}                    span 트리·워터폴·위임 그래프
GET  /v1/traces/{id}/io                 입력·출력 추출 (골든셋 승격용)
GET  /v1/sessions/{id}                  세션(a2a.context.id 등) 내 trace
POST /v1/replay                         trace 의 chat span 을 다른 모델·파라미터로 재실행해 비교
GET  /v1/dashboards/llm · /v1/costs     LLM 대시보드 · 비용 귀속
POST|GET /v1/slos · GET /v1/slos/{id}/status · POST|GET /v1/alerts/rules · GET /v1/alerts/events · POST /v1/alerts/evaluate
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext, otel
from units.ob import queries
from units.ob.ch import ClickHouse
from units.ob.dsl import DslError, compile_query
from units.ob.store import ObStore

log = logging.getLogger("atlas.ob")
MANIFEST = Path(__file__).with_name("atlas-agent.json")
WINDOWS = {"1h": 3600, "6h": 21600}
ECHO: deque[dict[str, Any]] = deque(maxlen=50)


def ch() -> ClickHouse:
    return ClickHouse()


def store() -> ObStore:
    return ObStore(agent.resources["engine"])


def bad_request(exc: Exception) -> HTTPException:
    return HTTPException(422, str(exc))


router = APIRouter(prefix="/v1", tags=["observability"])


@router.get("/traces")
async def list_traces(q: str = "", since: int = Query(3600, ge=60, le=30 * 86400), limit: int = Query(50, ge=1, le=500)):
    try:
        return {"items": await queries.search(ch(), q, since, limit), "query": q}
    except DslError as exc:
        raise bad_request(exc) from exc


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    rows = await queries.spans(ch(), trace_id)
    if not rows:
        raise HTTPException(404, "trace not found")
    return {"trace_id": trace_id, **queries.tree(rows)}


@router.get("/traces/{trace_id}/io")
async def get_trace_io(trace_id: str):
    rows = await queries.spans(ch(), trace_id)
    if not rows:
        raise HTTPException(404, "trace not found")
    return {"trace_id": trace_id, **queries.trace_io(rows)}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    return {"session_id": session_id, "traces": await queries.session(ch(), session_id)}


class Replay(BaseModel):
    trace_id: str
    span_id: str | None = None          # 생략 시 첫 chat span
    model: str | None = None            # 재실행 alias (생략 시 원본)
    temperature: float | None = None


@router.post("/replay")
async def replay(body: Replay):
    """trace → Playground (FR-OB-DEBUG-01): 기록된 입력 메시지로 다른 모델·파라미터를 돌려 원본과 나란히 비교."""
    rows = await queries.spans(ch(), body.trace_id)
    chats = [r for r in rows if r["name"].startswith("chat ") and r["service"] == "atlas-gateway"]
    span = next((r for r in chats if r["span_id"] == body.span_id), None) if body.span_id else (chats[0] if chats else None)
    if span is None:
        raise HTTPException(404, "chat span 없음")
    attrs = span["attributes"]
    if "gen_ai.input.messages" not in attrs:
        raise HTTPException(409, "본문이 기록되지 않은 span (ob.capture=metadata/none)")
    messages = json.loads(attrs["gen_ai.input.messages"])
    model = body.model or attrs.get("gen_ai.request.model")
    req: dict[str, Any] = {"model": model, "messages": messages}
    if body.temperature is not None:
        req["temperature"] = body.temperature
    gw = os.environ.get("ATLAS_GATEWAY_URL", "http://gateway:8080").rstrip("/")
    headers = otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}",
                           "x-atlas-tags": f"agent_id:ob-debug,trace:{body.trace_id}"})
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=60) as c:
        res = await c.post(f"{gw}/v1/chat/completions", json=req, headers=headers)
    if res.status_code != 200:
        raise HTTPException(502, f"gateway {res.status_code}: {res.text[:200]}")
    out = res.json()["choices"][0]["message"].get("content") or ""
    original = next((m.get("content", "") for m in reversed(json.loads(attrs.get("gen_ai.output.messages", "[]"))) if m.get("role") == "assistant"), "")
    return {"trace_id": body.trace_id, "span_id": span["span_id"],
            "original": {"model": attrs.get("gen_ai.request.model"), "output": original, "cost_usd": attrs.get("atlas.cost.usd")},
            "replay": {"model": model, "output": out, "cost_usd": res.headers.get("x-atlas-cost-usd"),
                       "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "trace_id": res.headers.get("x-atlas-trace-id")},
            "same_output": out == original}


@router.get("/dashboards/llm")
async def dashboard(since: int = Query(3600, ge=60, le=30 * 86400), group_by: str | None = None):
    if group_by and group_by not in queries.GROUPS:
        raise HTTPException(422, f"group_by: {', '.join(queries.GROUPS)}")
    return await queries.llm_dashboard(ch(), since, group_by)


class ErasureIn(BaseModel):
    subject: str = Field(min_length=3, max_length=256)


@router.post("/erasure")
async def erasure(body: ErasureIn):
    """삭제권 전파 (FR-X-KB-08): 주체 식별자가 들어간 span 속성 값을 '[erased]' 로 바꾼다(동기 mutation). 반환 = 영향 span 수."""
    where = "position(toString(SpanAttributes), {s:String}) > 0"
    rows = await ch().query(f"SELECT count() AS n FROM otel_traces WHERE {where}", {"s": body.subject})
    n = int(rows[0]["n"]) if rows else 0
    if n:
        await ch().command(f"ALTER TABLE otel_traces UPDATE SpanAttributes = mapApply((k, v) -> (k, replaceAll(v, {{s:String}}, '[erased]')), SpanAttributes) "
                           f"WHERE {where} SETTINGS mutations_sync = 2", {"s": body.subject})
    left = await ch().query(f"SELECT count() AS n FROM otel_traces WHERE {where}", {"s": body.subject})
    return {"store": "traces", "matched": n, "remaining": int(left[0]["n"]) if left else 0}


@router.get("/costs")
async def costs(group_by: str = "agent", since: int = Query(86400, ge=60, le=30 * 86400)):
    try:
        return {"group_by": group_by, "items": await queries.costs(ch(), since, group_by)}
    except ValueError as exc:
        raise bad_request(exc) from exc


class SloIn(BaseModel):
    name: str
    kind: str = Field(pattern="^(latency|error_rate)$")
    objective: float = Field(gt=0, lt=1)
    threshold_ms: float | None = Field(None, gt=0)
    window_days: int = Field(28, ge=1, le=90)
    filter: str = 'service = "atlas-gateway" AND name ~ "chat "'


@router.post("/slos", status_code=201)
async def create_slo(body: SloIn):
    if body.kind == "latency" and not body.threshold_ms:
        raise HTTPException(422, "latency SLO 는 threshold_ms 필요")
    try:
        compile_query(body.filter)
    except DslError as exc:
        raise bad_request(exc) from exc
    row = store().add_slo(**body.model_dump())
    return {"id": row.id, **body.model_dump()}


@router.get("/slos")
async def list_slos():
    return {"items": [{"id": r.id, "name": r.name, "kind": r.kind, "objective": r.objective, "threshold_ms": r.threshold_ms,
                       "window_days": r.window_days, "filter": r.filter} for r in store().slos()]}


async def slo_status(sid: str) -> dict[str, Any]:
    """SLI·오류 예산·멀티 윈도 번레이트(1h/6h) (FR-OB-DASH-03)."""
    s = store().slo(sid)
    if s is None:
        raise HTTPException(404, "slo not found")
    budget = 1 - s.objective
    out: dict[str, Any] = {"id": s.id, "name": s.name, "objective": s.objective, "windows": {}}
    for label, secs in {**WINDOWS, f"{s.window_days}d": s.window_days * 86400}.items():
        total, bad = await queries.sli_counts(ch(), s.kind, s.threshold_ms, s.filter, secs)
        frac = bad / total if total else 0.0
        out["windows"][label] = {"total": total, "bad": bad, "sli": round(1 - frac, 6), "burn_rate": round(frac / budget, 3)}
    w = out["windows"][f"{s.window_days}d"]
    out["error_budget_remaining"] = round(1 - (w["bad"] / w["total"] / budget if w["total"] else 0), 4)
    return out


@router.get("/slos/{sid}/status")
async def get_slo_status(sid: str):
    return await slo_status(sid)


class RuleIn(BaseModel):
    name: str
    expr: dict[str, Any]          # {"type":"slo_burn","slo_id":..,"short":14.4,"long":6} | {"type":"metric","metric":"error_rate|p95_ms|cost_usd","op":">","value":..,"window":"1h"}
    severity: str = Field("warning", pattern="^(info|warning|critical)$")
    webhook_url: str | None = None


@router.post("/alerts/rules", status_code=201)
async def create_rule(body: RuleIn):
    t = body.expr.get("type")
    if t == "slo_burn" and store().slo(body.expr.get("slo_id", "")) is None:
        raise HTTPException(422, "slo_burn: 존재하는 slo_id 필요")
    if t == "metric" and body.expr.get("metric") not in ("error_rate", "p95_ms", "cost_usd"):
        raise HTTPException(422, "metric: error_rate | p95_ms | cost_usd")
    if t not in ("slo_burn", "metric"):
        raise HTTPException(422, "expr.type: slo_burn | metric")
    row = store().add_rule(name=body.name, expr=body.expr, severity=body.severity, webhook_url=body.webhook_url)
    return {"id": row.id, **body.model_dump()}


@router.get("/alerts/rules")
async def list_rules():
    return {"items": [{"id": r.id, "name": r.name, "expr": r.expr, "severity": r.severity, "state": r.state, "last_value": r.last_value}
                      for r in store().rules()]}


async def rule_value(rule) -> tuple[bool, float, list[str]]:
    e = rule.expr
    if e["type"] == "slo_burn":
        st = await slo_status(e["slo_id"])
        short, long_ = st["windows"]["1h"]["burn_rate"], st["windows"]["6h"]["burn_rate"]
        s = store().slo(e["slo_id"])
        traces = await queries.bad_examples(ch(), s.kind, s.threshold_ms, s.filter, 3600)
        return short > float(e.get("short", 14.4)) and long_ > float(e.get("long", 6)), short, traces
    secs = WINDOWS.get(e.get("window", "1h"), 3600)
    dash = await queries.llm_dashboard(ch(), secs, None)
    row = dash["summary"][0] if dash["summary"] else {"error_rate": 0, "p95_ms": 0, "cost_usd": 0}
    v = float(row[e["metric"]])
    hit = v > float(e["value"]) if e.get("op", ">") == ">" else v < float(e["value"])
    kind = "error_rate" if e["metric"] == "error_rate" else "latency"
    traces = await queries.bad_examples(ch(), kind, float(e["value"]) if e["metric"] == "p95_ms" else None,
                                        'service = "atlas-gateway" AND name ~ "chat "', secs)
    return hit, v, traces


async def send_webhook(url: str, payload: dict[str, Any]) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            (await c.post(url, json=payload)).raise_for_status()
        return "sent"
    except httpx.HTTPError:
        return "failed"


async def evaluate_rules() -> list[dict[str, Any]]:
    """경보 평가 — 상태가 바뀔 때만 알린다(중복 묶음). 경보에 대표 trace 링크 최대 5개 (FR-OB-DASH-04)."""
    out = []
    for r in store().rules():
        try:
            hit, value, traces = await rule_value(r)
        except Exception as exc:  # noqa: BLE001 — 규칙 하나의 오류가 평가 전체를 막지 않는다
            log.warning("alert rule %s failed: %s", r.name, exc)
            continue
        new = "firing" if hit else "ok"
        if new != r.state:
            status = "firing" if hit else "resolved"
            base = os.environ.get("ATLAS_OB_PUBLIC_URL", "http://agent-ob:8000")
            payload = {"rule": r.name, "status": status, "severity": r.severity, "value": value, "expr": r.expr,
                       "traces": [f"{base}/v1/traces/{t}" for t in traces[:5]]}
            delivered = await send_webhook(r.webhook_url, payload) if r.webhook_url else "none"
            store().add_event(r.id, status, value, payload, delivered)
            out.append({**payload, "delivered": delivered})
        store().set_rule_state(r.id, new, value)
    return out


@router.post("/alerts/evaluate")
async def evaluate_now():
    return {"transitions": await evaluate_rules()}


@router.get("/alerts/events")
async def list_events(rule_id: str | None = None):
    return {"items": [{"rule_id": e.rule_id, "status": e.status, "value": e.value, "delivered": e.delivered, "payload": e.payload,
                       "created_at": e.created_at.isoformat()} for e in store().events(rule_id)]}


@router.post("/alerts/echo")
async def echo_receiver(payload: dict[str, Any]):
    """개발용 웹훅 수신기 (ATLAS_OB_ECHO_RECEIVER=true 일 때만)."""
    if os.environ.get("ATLAS_OB_ECHO_RECEIVER") != "true":
        raise HTTPException(404, "disabled")
    ECHO.appendleft(payload)
    return {"ok": True}


@router.get("/alerts/echo")
async def echo_list():
    if os.environ.get("ATLAS_OB_ECHO_RECEIVER") != "true":
        raise HTTPException(404, "disabled")
    return {"items": list(ECHO)}


async def alert_loop() -> None:
    interval = float(os.environ.get("ATLAS_OB_ALERT_INTERVAL", "30"))
    while True:
        await asyncio.sleep(interval)
        try:
            await evaluate_rules()
        except Exception as exc:  # noqa: BLE001
            log.warning("alert evaluation failed: %s", exc)


async def ch_ready() -> None:
    await ch().ping()


async def search_skill(ctx: TaskContext) -> dict[str, Any]:
    if "q" not in ctx.data and not ctx.text:
        raise InputRequired("data.q(검색 DSL) 가 필요합니다")
    return {"items": await queries.search(ch(), ctx.data.get("q", ctx.text), int(ctx.data.get("since", 3600)), int(ctx.data.get("limit", 20)))}


agent = AgentSpec.from_file(MANIFEST, description="OB-TRACE — trace 검색·트리·세션, 재실행 비교, LLM 대시보드·비용·SLO·경보.",
                            skills=[Skill(id="ob.trace", name="Trace search", handler=search_skill, description="필터 DSL 로 trace 검색")],
                            readiness_checks=[ch_ready], routers=[router], background=[alert_loop])
