"""X-RPT Usage Reports & Audit (FR-X-RPT-01~06·08).

- 계측 (RPT-01): X-NTF webhook 이 PLT·GW 스트림을 그대로 넘긴다(HMAC 서명) → gw.request.completed(토큰·비용)·plt.sbx.run.completed(CPU 초)
  + 직접 수집 API(도구 호출·저장량 등). 차원 = tenant·project·agent·unit·model·tags, 원천 id 로 멱등.
- 비용 리포트 (RPT-02): GET /v1/reports/usage?group_by=project,model&from&to&currency&format=json|csv|pdf (통화 환산표).
- 차지백 (RPT-03): 요율표(plan·meter·단가·마크업) + 프로젝트→플랜·부서 → 월 마감이 인보이스 초안.
- 감사 (RPT-04): Manager 감사(audit.recorded)·에이전트 관리 이벤트를 통합 — 해시 체인, 추가 전용(UPDATE/DELETE 트리거 거부),
  WORM 사본(봉인 세그먼트 0444 + sha256), 보존 audit_days ≥ 365, 행위자·행위·자원·기간 검색.
- SIEM (RPT-05): OCSF(API Activity 6003)·OTel Logs 변환 → Splunk HEC·HTTP 싱크, 실패하면 커서가 멈춰 버퍼(재전송).
- 월간 보고서 (RPT-06): cost_per_task 추세·task_success_rate·guardrail_block_rate·MAT 점수 4차트 HTML/PDF → plt.rpt.report.ready → X-NTF 이메일.
- 이상 탐지 (RPT-08): 에이전트별 시간 창 EWMA·z-score(>3) → plt.rpt.usage.anomaly(원인 요청 id 링크).
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from atlas_agent_core import AgentSpec, Skill, TaskContext, events, otel, secrets
from units.rpt import render
from units.rpt.store import (
    AnomalyRow,
    AuditRow,
    InvoiceRow,
    PlanRow,
    RateRow,
    ReportRow,
    SinkRow,
    UsageRow,
)

MANIFEST = Path(__file__).with_name("atlas-agent.json")
TRANSPORT: httpx.AsyncBaseTransport | None = None
router = APIRouter(prefix="/v1", tags=["reports"])
log = logging.getLogger("atlas.rpt")
_LOCK = asyncio.Lock()
METERS = ("requests", "tokens_in", "tokens_out", "tokens_cached", "tool_calls", "sbx_cpu_s", "storage_gb_h", "tasks")


def cfg() -> dict[str, Any]:
    return agent.manifest["spec"]["config"]


def engine():
    return agent.resources["engine"]


def url(env: str, default: str) -> str:
    return (os.environ.get(env) or default).rstrip("/")


def aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def parse_ts(v: Any) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, UTC)
    if isinstance(v, str) and v:
        return aware(datetime.fromisoformat(v))
    return datetime.now(UTC)


async def require_admin(authorization: str | None) -> None:
    tok = await secrets.get("rpt-admin-token", env="ATLAS_RPT_ADMIN_TOKEN", dev_default="local-rpt-admin")
    if not tok or "CHANGE_ME" in tok or not hmac.compare_digest(authorization or "", f"Bearer {tok}"):
        raise HTTPException(401, "관리 토큰 필요")


def worm_dir() -> Path:
    return Path(os.environ.get("ATLAS_RPT_WORM_DIR") or "data/rpt/worm")


def retention_days() -> int:
    return max(365, int(cfg().get("auditRetentionDays", 365)))      # 규제 하한 365일 (X-RPT-04)


# ---- 계측 (RPT-01) ----

def add_usage(rows: list[dict[str, Any]]) -> int:
    n = 0
    with Session(engine()) as s:
        for r in rows:
            try:
                with s.begin_nested():
                    s.add(UsageRow(**r))
                n += 1
            except IntegrityError:
                pass                                              # 같은 원천 id = 재전송 → 무시 (멱등)
        s.commit()
    return n


def usage_from_gateway(ev_id: str, d: dict[str, Any], ts: datetime) -> list[dict[str, Any]]:
    tags = d.get("tags") or {}
    base = {"ts": ts, "tenant": d.get("tenant") or "default", "project": d.get("project") or "default", "unit_id": tags.get("unit"),
            "agent_id": tags.get("agent") or d.get("key_prefix"), "model": d.get("model") or d.get("alias"), "tags": tags, "source": "gateway",
            "source_id": d.get("request_id") or ev_id}
    out = [{**base, "meter": "requests", "qty": 1.0, "cost_usd": float(d.get("cost_usd") or 0)}]
    for meter, key in (("tokens_in", "input"), ("tokens_out", "output"), ("tokens_cached", "cached_read")):
        if d.get(key):
            out.append({**base, "meter": meter, "qty": float(d[key]), "cost_usd": 0.0})
    return out


def usage_from_sandbox(ev_id: str, d: dict[str, Any], ts: datetime, tenant: str) -> list[dict[str, Any]]:
    cpu = float(d.get("duration_ms") or 0) / 1000
    return [{"ts": ts, "tenant": tenant, "project": d.get("project") or "default", "unit_id": "X-SBX", "agent_id": d.get("agent") or "sbx",
             "model": None, "meter": "sbx_cpu_s", "qty": cpu, "cost_usd": round(cpu * float(cfg()["sbxCpuUsdPerSecond"]), 8),
             "tags": {"lang": d.get("lang"), "status": d.get("status")}, "source": "sandbox", "source_id": d.get("run") or ev_id}]


class UsageEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: str | float | None = None
    tenant: str = "default"
    project: str = "default"
    unit_id: str | None = None
    agent_id: str | None = None
    model: str | None = None
    meter: str
    qty: float
    cost_usd: float = 0.0
    tags: dict[str, Any] = {}


class UsageBatch(BaseModel):
    events: list[UsageEvent] = Field(max_length=5000)


@router.post("/usage", status_code=202)
async def ingest_usage(body: UsageBatch):
    """직접 수집 (OpenMeter 식) — 도구 호출·저장량 등 원천이 스스로 보고."""
    bad = [e.meter for e in body.events if e.meter not in METERS]
    if bad:
        raise HTTPException(422, f"meter = {METERS} (받은 값 {sorted(set(bad))})")
    n = add_usage([{**e.model_dump(exclude={"id", "ts"}), "ts": parse_ts(e.ts), "source": "api", "source_id": e.id} for e in body.events])
    return {"accepted": n, "duplicates": len(body.events) - n}


# ---- 감사 (RPT-04) ----

def chain_hash(prev: str, r: dict[str, Any]) -> str:
    body = json.dumps({k: r[k] for k in ("seq", "id", "ts", "tenant", "actor", "actor_type", "action", "resource", "result", "before", "after", "ip",
                                          "source", "source_id")}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((prev + body).encode()).hexdigest()


def row_dict(a: AuditRow) -> dict[str, Any]:
    return {"seq": a.seq, "id": a.id, "ts": aware(a.ts).isoformat(), "tenant": a.tenant, "actor": a.actor, "actor_type": a.actor_type, "action": a.action,
            "resource": a.resource, "result": a.result, "before": a.before, "after": a.after, "ip": a.ip, "source": a.source, "source_id": a.source_id,
            "prev_hash": a.prev_hash, "hash": a.hash}


async def append_audit(entries: list[dict[str, Any]]) -> int:
    n = 0
    async with _LOCK:
        with Session(engine()) as s, s.begin():
            last = s.scalar(select(AuditRow).order_by(AuditRow.seq.desc()).limit(1))
            prev, seq = (last.hash, last.seq) if last else ("", 0)
            for e in entries:
                if s.scalar(select(AuditRow.seq).where(AuditRow.source_id == e["source_id"])):
                    continue
                seq += 1
                r = {"seq": seq, "id": str(uuid.uuid4()), "result": "success", "before": None, "after": None, "ip": "", **e}
                r["ts"] = aware(r["ts"]).isoformat()
                h = chain_hash(prev, r)
                s.add(AuditRow(**{**r, "ts": parse_ts(r["ts"])}, prev_hash=prev, hash=h))
                prev = h
                n += 1
    return n


def audit_from_manager(d: dict[str, Any]) -> dict[str, Any]:
    try:
        detail = json.loads(d.get("detail") or "{}")
    except ValueError:
        detail = {"raw": d.get("detail")}
    return {"ts": parse_ts(d.get("ts")), "tenant": d.get("org") or "default", "actor": d.get("actorId") or "anonymous", "actor_type": d.get("actorType") or "user",
            "action": d.get("action", ""), "resource": d.get("resource", ""), "result": d.get("result", "success"),
            "before": {"hash": d.get("beforeHash")}, "after": {"hash": d.get("afterHash"), "detail": detail, "manager_hash": d.get("hash")},
            "ip": d.get("actorIp") or "", "source": "manager", "source_id": f"manager:{d.get('id')}"}


def audit_from_event(ev_id: str, topic: str, d: dict[str, Any], ts: datetime, org: str) -> dict[str, Any]:
    res = next((f"{k}:{d[k]}" for k in ("resource", "template", "install", "server", "run", "id", "subject_sha256") if d.get(k)), topic)
    return {"ts": ts, "tenant": org, "actor": str(d.get("by") or d.get("actor") or d.get("agent") or "system"), "actor_type": "agent" if d.get("agent") else "system",
            "action": topic, "resource": res[:256], "result": str(d.get("decision") or d.get("status") or "success")[:32],
            "after": {k: v for k, v in d.items() if k not in ("org", "tenant")}, "source": "event", "source_id": f"event:{ev_id}"}


def seal_worm() -> dict[str, Any]:
    """새 감사 행을 봉인 세그먼트로 — 한 번 쓰고 0444 (WORM 사본, 운영은 오브젝트 스토리지 Object Lock 으로 교체 지점)."""
    d = worm_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".seal.lock", "a") as lk:   # 워커·주기 작업이 동시에 봉인하면 겹치는 세그먼트가 생긴다 — 프로세스 간 배타 잠금
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _seal(d)


def _seal(d: Path) -> dict[str, Any]:
    last = max((int(p.stem.split("-")[2]) for p in d.glob("audit-*.jsonl")), default=0)
    with Session(engine()) as s:
        new = list(s.scalars(select(AuditRow).where(AuditRow.seq > last).order_by(AuditRow.seq)))
    if not new:
        return {"sealed": 0, "last_seq": last}
    p = d / f"audit-{new[0].seq:012d}-{new[-1].seq:012d}.jsonl"
    data = "".join(json.dumps(row_dict(a), ensure_ascii=False, sort_keys=True) + "\n" for a in new).encode()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    sig = p.with_suffix(".sha256")
    fd = os.open(sig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "w") as f:
        f.write(hashlib.sha256(data).hexdigest())
    return {"sealed": len(new), "last_seq": new[-1].seq, "segment": p.name}


@router.get("/audit")
async def search_audit(tenant: str = "default", actor: str | None = None, action: str | None = None, resource: str | None = None,
                       since: str | None = None, until: str | None = None, limit: int = 100):
    """누가·언제·무엇을 — 행위는 접두어 일치(예: agent. · plt.mkt.)."""
    with Session(engine()) as s:
        q = select(AuditRow).where(AuditRow.tenant == tenant)
        if actor:
            q = q.where(AuditRow.actor == actor)
        if action:
            q = q.where(AuditRow.action.startswith(action))
        if resource:
            q = q.where(AuditRow.resource.startswith(resource))
        if since:
            q = q.where(AuditRow.ts >= parse_ts(since))
        if until:
            q = q.where(AuditRow.ts < parse_ts(until))
        items = [row_dict(a) for a in s.scalars(q.order_by(AuditRow.seq.desc()).limit(min(limit, 1000)))]
    return {"items": items, "retention_days": retention_days()}


@router.get("/audit/verify")
async def verify_audit():
    seal_worm()
    prev, n, broken = "", 0, None
    db: dict[int, str] = {}
    with Session(engine()) as s:
        for a in s.scalars(select(AuditRow).order_by(AuditRow.seq)):
            r = row_dict(a)
            if a.prev_hash != prev or chain_hash(prev, r) != a.hash:
                broken = broken or a.seq
            prev, n = a.hash, n + 1
            db[a.seq] = a.hash
    worm_bad, segs, dup, seen = [], 0, 0, {}
    for p in sorted(worm_dir().glob("audit-*.jsonl")):
        segs += 1
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != p.with_suffix(".sha256").read_text().strip():
            worm_bad.append(p.name)
        for line in data.decode().splitlines():
            x = json.loads(line)
            if db.get(x["seq"]) != x["hash"] or seen.get(x["seq"], x["hash"]) != x["hash"]:
                worm_bad.append(f"seq {x['seq']}")
            elif x["seq"] in seen:
                dup += 1   # 같은 행이 겹친 세그먼트에 또 봉인됨(잠금 도입 전) — 내용이 같으면 무결성 문제 아님
            seen[x["seq"]] = x["hash"]
    worm_n = len(seen)
    return {"events": n, "valid": broken is None, "broken_at": broken, "head": prev, "worm": {"segments": segs, "events": worm_n, "match": not worm_bad and worm_n == n,
            "mismatches": worm_bad[:20], "duplicates": dup}, "retention_days": retention_days()}


# ---- X-NTF webhook 수집 ----

@router.post("/ingest/events", status_code=202)
async def ingest_events(request: Request, x_atlas_signature: str | None = Header(None), x_atlas_event: str | None = Header(None)):
    body = await request.body()
    secret = await secrets.get("rpt-ingest-secret", env="ATLAS_RPT_INGEST_SECRET", dev_default="local-rpt-ingest")
    try:
        parts = dict(p.split("=", 1) for p in (x_atlas_signature or "").split(","))
        t = int(parts["t"])
        want = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(want, parts["v1"]) and abs(time.time() - t) < 300
    except (KeyError, ValueError):
        ok = False
    if not ok:
        raise HTTPException(401, "서명 검증 실패 (X-Atlas-Signature)")
    m = json.loads(body)
    topic, d, org, eid = m.get("topic") or x_atlas_event or "", m.get("data") or {}, m.get("org") or "default", m.get("id") or str(uuid.uuid4())
    ts = parse_ts(m.get("time"))
    usage, audit = [], []
    if topic == "gw.request.completed":
        usage = usage_from_gateway(eid, d, ts)
    elif topic == "plt.sbx.run.completed":
        usage = usage_from_sandbox(eid, d, ts, org)
    elif topic == "audit.recorded":
        audit = [audit_from_manager(d)]
    elif any(topic.startswith(p) for p in cfg()["auditTopics"]):
        audit = [audit_from_event(eid, topic, d, ts, org)]
    return {"usage": add_usage(usage) if usage else 0, "audit": await append_audit(audit) if audit else 0}


# ---- 비용 리포트 (RPT-02) ----

def dim(u: UsageRow, k: str) -> str:
    if k.startswith("tag:"):
        return str((u.tags or {}).get(k[4:], ""))
    return {"tenant": u.tenant, "project": u.project, "model": u.model or "", "agent": u.agent_id or "", "unit": u.unit_id or "", "meter": u.meter,
            "day": aware(u.ts).strftime("%Y-%m-%d"), "month": aware(u.ts).strftime("%Y-%m")}[k]


GROUPS = ("tenant", "project", "model", "agent", "unit", "meter", "day", "month")


def usage_rows(tenant: str | None, since: datetime, until: datetime, project: str | None = None) -> list[UsageRow]:
    with Session(engine()) as s:
        q = select(UsageRow).where(UsageRow.ts >= since, UsageRow.ts < until)
        if tenant:
            q = q.where(UsageRow.tenant == tenant)
        if project:
            q = q.where(UsageRow.project == project)
        return list(s.scalars(q))


def aggregate(rows: list[UsageRow], group_by: list[str], rate: float) -> list[dict[str, Any]]:
    acc: dict[tuple, dict[str, Any]] = {}
    for u in rows:
        key = tuple(dim(u, g) for g in group_by)
        a = acc.setdefault(key, {**dict(zip(group_by, key)), "cost": 0.0, **{m: 0.0 for m in METERS}})
        a["cost"] += u.cost_usd * rate
        a[u.meter] += u.qty
    out = []
    for a in acc.values():
        out.append({k: (round(v, 8) if k == "cost" else round(v, 4) if isinstance(v, float) else v) for k, v in a.items()})   # 비용은 소액 요청도 보존
    return sorted(out, key=lambda r: -r["cost"])


@router.get("/reports/usage")
async def usage_report(tenant: str | None = "default", group_by: str = "project,model", since: str | None = None, until: str | None = None,
                       currency: str = "USD", format: str = "json", project: str | None = None):
    gb = [g for g in group_by.split(",") if g]
    bad = [g for g in gb if g not in GROUPS and not g.startswith("tag:")]
    if bad:
        raise HTTPException(422, f"group_by = {GROUPS} 또는 tag:<키> (받은 값 {bad})")
    rates = cfg()["currencies"]
    if currency not in rates:
        raise HTTPException(422, f"currency = {sorted(rates)}")
    t1 = parse_ts(until) if until else datetime.now(UTC) + timedelta(seconds=1)
    t0 = parse_ts(since) if since else t1 - timedelta(days=30)
    items = aggregate(usage_rows(tenant, t0, t1, project), gb, float(rates[currency]))
    meta = {"group_by": gb, "from": t0.isoformat(), "to": t1.isoformat(), "currency": currency, "rate": rates[currency],
            "total": round(sum(i["cost"] for i in items), 8)}
    if format == "csv":
        return Response(render.to_csv(items), media_type="text/csv; charset=utf-8",
                        headers={"content-disposition": f'attachment; filename="usage-{t0:%Y%m%d}-{t1:%Y%m%d}.csv"'})
    if format == "pdf":
        cols = [*gb, "cost", "requests", "tokens_in", "tokens_out"]
        pdf = render.to_pdf(f"ProdAI Atlas usage {t0:%Y-%m-%d}..{t1:%Y-%m-%d} ({currency}) total {meta['total']}",
                            [{c: i.get(c) for c in cols} for i in items], [("/".join(str(i[g]) for g in gb), i["cost"]) for i in items])
        return Response(pdf, media_type="application/pdf", headers={"content-disposition": 'attachment; filename="usage.pdf"'})
    if format == "parquet":
        raise HTTPException(415, "parquet 내보내기는 pyarrow 미포함 이미지라 미지원 — csv 또는 pdf")
    if format != "json":
        raise HTTPException(422, "format = json | csv | pdf")
    return {**meta, "items": items}


# ---- 차지백 (RPT-03) ----

class MeterRate(BaseModel):
    unit_price: float = Field(ge=0)
    markup_pct: float = Field(0, ge=0, le=500)


class RatesIn(BaseModel):
    currency: str = "USD"
    meters: dict[str, MeterRate]


@router.put("/rates/{plan}")
async def put_rates(plan: str, body: RatesIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    bad = [m for m in body.meters if m not in (*METERS, "cost")]
    if bad:
        raise HTTPException(422, f"meter = {METERS} 또는 cost(원가 배수)")
    with Session(engine()) as s, s.begin():
        for m, r in body.meters.items():
            s.merge(RateRow(plan=plan, meter=m, unit_price=r.unit_price, markup_pct=r.markup_pct, currency=body.currency))
    return {"plan": plan, "meters": {m: r.model_dump() for m, r in body.meters.items()}, "currency": body.currency}


@router.get("/rates")
async def get_rates():
    with Session(engine()) as s:
        out: dict[str, Any] = {}
        for r in s.scalars(select(RateRow)):
            out.setdefault(r.plan, {"currency": r.currency, "meters": {}})["meters"][r.meter] = {"unit_price": r.unit_price, "markup_pct": r.markup_pct}
    return out


class PlanIn(BaseModel):
    plan: str
    department: str = ""


@router.put("/plans/{tenant}/{project}")
async def put_plan(tenant: str, project: str, body: PlanIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    with Session(engine()) as s, s.begin():
        s.merge(PlanRow(tenant=tenant, project=project, plan=body.plan, department=body.department))
    return {"tenant": tenant, "project": project, **body.model_dump()}


def month_range(month: str) -> tuple[datetime, datetime]:
    y, m = (int(x) for x in month.split("-"))
    t0 = datetime(y, m, 1, tzinfo=UTC)
    return t0, datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)


class CloseIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    tenant: str | None = None


@router.post("/chargeback/close")
async def close_month(body: CloseIn, authorization: str | None = Header(None)):
    """월 마감 → 프로젝트(부서)별 인보이스 초안. 요율 없는 미터는 원가 통과(cost 미터 배수 적용), 마크업은 미터별."""
    await require_admin(authorization)
    t0, t1 = month_range(body.month)
    rows = usage_rows(body.tenant, t0, t1)
    with Session(engine()) as s:
        plans = {(p.tenant, p.project): p for p in s.scalars(select(PlanRow))}
        rates: dict[str, dict[str, RateRow]] = {}
        for r in s.scalars(select(RateRow)):
            rates.setdefault(r.plan, {})[r.meter] = r
    groups: dict[tuple[str, str], list[UsageRow]] = {}
    for u in rows:
        groups.setdefault((u.tenant, u.project), []).append(u)
    out = []
    with Session(engine()) as s, s.begin():
        for (tenant, project), us in sorted(groups.items()):
            p = plans.get((tenant, project))
            plan = p.plan if p else cfg()["defaultPlan"]
            pr = rates.get(plan, {})
            cur = next(iter(pr.values())).currency if pr else "USD"
            lines, subtotal, markup = [], 0.0, 0.0
            by_meter: dict[str, tuple[float, float]] = {}
            for u in us:
                q, c = by_meter.get(u.meter, (0.0, 0.0))
                by_meter[u.meter] = (q + u.qty, c + u.cost_usd)
            for meter, (q, c) in sorted(by_meter.items()):
                if meter in pr:
                    amount, mk = q * pr[meter].unit_price, pr[meter].markup_pct
                elif "cost" in pr:
                    amount, mk = c * pr["cost"].unit_price, pr["cost"].markup_pct
                else:
                    amount, mk = c, 0.0
                if amount == 0:
                    continue
                lines.append({"meter": meter, "qty": round(q, 4), "cost_usd": round(c, 8), "amount": round(amount, 6), "markup_pct": mk,
                              "markup": round(amount * mk / 100, 6)})
                subtotal += amount
                markup += amount * mk / 100
            row = s.scalar(select(InvoiceRow).where(InvoiceRow.month == body.month, InvoiceRow.tenant == tenant, InvoiceRow.project == project))
            if row and row.status != "draft":
                continue
            row = row or InvoiceRow(month=body.month, tenant=tenant, project=project)
            row.plan, row.department, row.lines, row.currency = plan, p.department if p else "", lines, cur
            row.subtotal, row.markup, row.total, row.status = round(subtotal, 6), round(markup, 6), round(subtotal + markup, 6), "draft"
            s.add(row)
            out.append({"tenant": tenant, "project": project, "plan": plan, "total": row.total, "currency": cur, "lines": len(lines)})
    await events.publish(agent, "plt.rpt.chargeback.closed", {"month": body.month, "invoices": len(out)}, body.tenant or "default")
    return {"month": body.month, "invoices": out}


@router.get("/invoices")
async def invoices(month: str | None = None, tenant: str | None = None, format: str = "json"):
    with Session(engine()) as s:
        q = select(InvoiceRow)
        if month:
            q = q.where(InvoiceRow.month == month)
        if tenant:
            q = q.where(InvoiceRow.tenant == tenant)
        items = [{"id": i.id, "month": i.month, "tenant": i.tenant, "project": i.project, "department": i.department, "plan": i.plan, "lines": i.lines,
                  "subtotal": i.subtotal, "markup": i.markup, "total": i.total, "currency": i.currency, "status": i.status} for i in s.scalars(q)]
    if format == "csv":
        flat = [{k: v for k, v in i.items() if k != "lines"} for i in items]
        return Response(render.to_csv(flat), media_type="text/csv; charset=utf-8")
    return {"items": items}


# ---- SIEM (RPT-05) ----

def ocsf(a: AuditRow) -> dict[str, Any]:
    return {"class_uid": 6003, "class_name": "API Activity", "category_uid": 6, "activity_name": a.action, "activity_id": 99,
            "time": int(aware(a.ts).timestamp() * 1000), "severity_id": 1, "status": a.result,
            "actor": {"user": {"name": a.actor, "type": a.actor_type}}, "api": {"operation": a.action},
            "resources": [{"name": a.resource}], "src_endpoint": {"ip": a.ip}, "tenant_uid": a.tenant,
            "metadata": {"version": "1.3.0", "uid": a.id, "product": {"name": "ProdAI Atlas", "vendor_name": "ProdAI"}, "sequence": a.seq},
            "unmapped": {"hash": a.hash, "prev_hash": a.prev_hash, "source": a.source}}


def otel_logs(batch: list[AuditRow]) -> dict[str, Any]:
    recs = [{"timeUnixNano": str(int(aware(a.ts).timestamp() * 1e9)), "severityText": "INFO", "body": {"stringValue": json.dumps(ocsf(a), ensure_ascii=False)},
             "attributes": [{"key": "atlas.audit.action", "value": {"stringValue": a.action}}, {"key": "atlas.tenant", "value": {"stringValue": a.tenant}}]}
            for a in batch]
    return {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "atlas-audit"}}]},
                              "scopeLogs": [{"scope": {"name": "x-rpt"}, "logRecords": recs}]}]}


class SinkIn(BaseModel):
    type: str = Field(pattern="^(splunk_hec|http)$")
    url: str
    format: str = Field("ocsf", pattern="^(ocsf|otel)$")
    secret: str | None = None             # Manager 비밀 이름 (HEC 토큰 등)
    filter: list[str] = []
    enabled: bool = True
    from_start: bool = True               # false = 지금 이후 감사만


@router.put("/siem/sinks/{sid}")
async def put_sink(sid: str, body: SinkIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    with Session(engine()) as s, s.begin():
        cur = s.get(SinkRow, sid)
        start = 0 if body.from_start else (s.scalar(select(func.max(AuditRow.seq))) or 0)
        row = cur or SinkRow(id=sid, cursor=start, sent=0, failures=0)
        row.type, row.url, row.format, row.secret, row.filter, row.enabled = body.type, body.url, body.format, body.secret, body.filter, body.enabled
        s.add(row)
    return next(x for x in (await sinks())["items"] if x["id"] == sid)


@router.get("/siem/sinks")
async def sinks():
    with Session(engine()) as s:
        head = s.scalar(select(func.max(AuditRow.seq))) or 0
        out = []
        for k in s.scalars(select(SinkRow)):
            q = select(func.count()).select_from(AuditRow).where(AuditRow.seq > k.cursor)
            if k.filter:                                  # 버퍼 = 이 싱크로 갈 (필터 일치) 미전송분
                q = q.where(or_(*[AuditRow.action.startswith(f) for f in k.filter]))
            pending = s.scalar(q) or 0
            out.append({"id": k.id, "type": k.type, "url": k.url, "format": k.format, "filter": k.filter, "enabled": k.enabled, "cursor": k.cursor,
                        "head": head, "buffered": pending, "sent": k.sent, "failures": k.failures, "last_error": k.last_error,
                        "last_sent_at": k.last_sent_at.isoformat() if k.last_sent_at else None})
    return {"items": out}


async def flush_sinks(max_batches: int = 20) -> list[dict[str, Any]]:
    """싱크마다 미전송분을 500건 묶음으로 비운다(호출당 최대 max_batches) — 실패하면 그 싱크는 다음 주기로."""
    out: list[dict[str, Any]] = []
    for _ in range(max_batches):
        got = await flush_once()
        out += got
        if not got or any(x["error"] for x in got):
            break
    return out


async def flush_once() -> list[dict[str, Any]]:
    out = []
    with Session(engine()) as s:
        ks = list(s.scalars(select(SinkRow).where(SinkRow.enabled.is_(True))))
    for k in ks:
        with Session(engine()) as s:
            batch = list(s.scalars(select(AuditRow).where(AuditRow.seq > k.cursor).order_by(AuditRow.seq).limit(500)))
        if not batch:
            continue
        send = [a for a in batch if not k.filter or any(a.action.startswith(f) for f in k.filter)]
        h = {"content-type": "application/json"}
        if k.secret:
            try:
                tok = await secrets.get(k.secret)
                h["authorization"] = f"Splunk {tok}" if k.type == "splunk_hec" else f"Bearer {tok}"
            except secrets.SecretUnavailable:
                h = {}
        err = None
        if send and h:
            if k.type == "splunk_hec":
                content = "\n".join(json.dumps({"time": aware(a.ts).timestamp(), "sourcetype": "ocsf:api_activity", "source": "prodai-atlas",
                                                "event": ocsf(a)}, ensure_ascii=False) for a in send)
            else:
                content = json.dumps(otel_logs(send) if k.format == "otel" else [ocsf(a) for a in send], ensure_ascii=False)
            try:
                async with httpx.AsyncClient(timeout=10, transport=TRANSPORT) as c:
                    r = await c.post(k.url, content=content.encode(), headers=h)
                if r.status_code >= 300:
                    err = f"{r.status_code}: {r.text[:200]}"
            except httpx.HTTPError as exc:
                err = f"{type(exc).__name__}: {exc}"[:300]
        elif send:
            err = f"싱크 비밀 {k.secret} 없음"
        with Session(engine()) as s, s.begin():
            row = s.get(SinkRow, k.id)
            if err:                                     # 커서를 그대로 두면 다음 주기에 같은 묶음을 다시 보낸다 (버퍼)
                row.failures, row.last_error = row.failures + 1, err
            else:
                row.cursor, row.sent, row.last_error, row.last_sent_at = batch[-1].seq, row.sent + len(send), None, datetime.now(UTC)
        out.append({"sink": k.id, "sent": 0 if err else len(send), "error": err})
    return out


@router.post("/siem/flush")
async def flush_now():
    """수동 비우기는 밀린 분을 끝까지(새 싱크는 감사 이력 전체를 재생) — 주기 작업만 호출당 상한."""
    return {"items": await flush_sinks(max_batches=1000)}


# ---- 이상 탐지 (RPT-08) ----

class ScanIn(BaseModel):
    window_s: int = Field(300, ge=60, le=86400)
    lookback: int = Field(48, ge=6, le=500)
    tenant: str = "default"
    z: float = Field(3.0, gt=0)


async def detect(body: ScanIn) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    w = body.window_s
    end = int(now.timestamp()) // w * w + w
    start = end - w * body.lookback
    series: dict[tuple[str, str], dict[int, float]] = {}
    traces: dict[tuple[str, str, int], list[str]] = {}
    for u in usage_rows(body.tenant, datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC)):
        meter = "tokens" if u.meter in ("tokens_in", "tokens_out") else u.meter
        if meter not in ("tokens", "tool_calls"):
            continue
        b = int(aware(u.ts).timestamp()) // w * w
        key = (u.agent_id or "unknown", meter)
        series.setdefault(key, {})
        series[key][b] = series[key].get(b, 0.0) + u.qty
        traces.setdefault((*key, b), []).append(u.source_id)
    found = []
    for (ag, meter), pts in series.items():
        first = min(pts)
        mean, var, seen = None, 0.0, 0
        for b in range(first, end, w):
            x = pts.get(b, 0.0)
            if mean is not None and seen >= int(cfg()["anomalyMinWindows"]):
                std = max(math.sqrt(var), 1.0, 0.1 * mean)
                z = (x - mean) / std
                if z > body.z:
                    found.append({"agent_id": ag, "meter": meter, "window_start": datetime.fromtimestamp(b, UTC), "value": x, "mean": round(mean, 4),
                                  "std": round(std, 4), "z": round(z, 2), "traces": sorted(set(traces.get((ag, meter, b), [])))[:20]})
                    continue                         # 이상 창은 기준선에 섞지 않는다
            a = float(cfg()["ewmaAlpha"])
            if mean is None:
                mean = x
            else:
                d = x - mean
                mean += a * d
                var = (1 - a) * (var + a * d * d)
            seen += 1
    out = []
    for f in found:
        with Session(engine()) as s:
            try:
                with s.begin():
                    s.add(AnomalyRow(tenant=body.tenant, **f))
            except IntegrityError:
                continue
        await events.publish(agent, "plt.rpt.usage.anomaly", {**{k: v for k, v in f.items() if k != "window_start"}, "window_start": f["window_start"].isoformat(),
                                                              "trace_links": [f"/svc/ob/v1/traces?request_id={t}" for t in f["traces"][:5]]}, body.tenant)
        out.append({**f, "window_start": f["window_start"].isoformat()})
    return out


@router.post("/anomaly/scan")
async def anomaly_scan(body: ScanIn):
    return {"detected": await detect(body)}


@router.get("/anomalies")
async def anomalies(tenant: str = "default", limit: int = 50):
    with Session(engine()) as s:
        rows = s.scalars(select(AnomalyRow).where(AnomalyRow.tenant == tenant).order_by(AnomalyRow.window_start.desc()).limit(min(limit, 500)))
        return {"items": [{"id": a.id, "agent_id": a.agent_id, "meter": a.meter, "window_start": aware(a.window_start).isoformat(), "value": a.value,
                           "mean": a.mean, "z": a.z, "traces": a.traces} for a in rows]}


# ---- 월간 보고서 (RPT-06) ----

async def mat_view(org: str, project: str) -> dict[str, Any]:
    base = url("ATLAS_MAT_URL", "http://agent-mat:8000")
    try:
        async with httpx.AsyncClient(timeout=60, transport=TRANSPORT) as c:
            band = (await c.get(f"{base}/v1/band", params={"org": org, "project": project}, headers=otel.inject({}))).json()
            mets = {m["name"]: m["value"] for m in (await c.get(f"{base}/v1/metrics", params={"org": org, "project": project})).json()["items"]}
        scores = list((band.get("score_by_attr") or {}).values())
        return {"mat_score": round(sum(scores) / len(scores), 1) if scores else None, "mat_stage": band.get("stage"),
                "task_success_rate": mets.get("task_success_rate"), "guardrail_block_rate": mets.get("guardrail_block_rate"), "ev_cost_per_task": mets.get("cost_per_task")}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return {"mat_score": None, "mat_stage": None, "task_success_rate": None, "guardrail_block_rate": None, "error": str(exc)[:200]}


class MonthlyIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    tenant: str = "default"
    project: str = "default"
    send_to: list[str] = []


async def ensure_rule(org: str, name: str, body: dict[str, Any]) -> str | None:
    mgr = url("ATLAS_MANAGER_URL", "")
    if not mgr:
        return None
    tok = os.environ.get("ATLAS_MANAGER_TOKEN", "")
    h = {"Authorization": f"Bearer {tok}"} if tok and "CHANGE_ME" not in tok else {}
    async with httpx.AsyncClient(timeout=10, transport=TRANSPORT) as c:
        r = await c.get(f"{mgr}/api/v1/orgs/{org}/notifications/rules", headers=h)
        if r.status_code == 403:      # 규칙 관리는 조직 관리자 몫 — 이벤트만 발행하고 규칙은 관리자가 만든다
            return None
        r.raise_for_status()
        cur = next((x for x in r.json().get("items", []) if x.get("name") == name), None)
        if cur:
            return cur["id"]
        r = await c.post(f"{mgr}/api/v1/orgs/{org}/notifications/rules", json={"name": name, **body}, headers=h)
        if r.status_code == 403:
            return None
        if r.status_code >= 300:
            raise httpx.HTTPError(f"rule {name}: {r.status_code} {r.text[:200]}")
        return r.json().get("id")


async def build_monthly(body: MonthlyIn) -> dict[str, Any]:
    t0, t1 = month_range(body.month)
    rows = usage_rows(body.tenant, t0, t1, body.project)
    days: dict[str, dict[str, float]] = {}
    for u in rows:
        d = days.setdefault(aware(u.ts).strftime("%Y-%m-%d"), {"cost": 0.0, "requests": 0.0, "tasks": 0.0})
        d["cost"] += u.cost_usd
        if u.meter in ("requests", "tasks"):
            d[u.meter] += u.qty
    cpt = [{"day": k, "value": round(v["cost"] / (v["tasks"] or v["requests"]), 8) if (v["tasks"] or v["requests"]) else None} for k, v in sorted(days.items())]
    m = await mat_view(body.tenant, body.project)
    with Session(engine()) as s:
        n_anom = s.scalar(select(func.count()).select_from(AnomalyRow).where(AnomalyRow.tenant == body.tenant, AnomalyRow.window_start >= t0,
                                                                              AnomalyRow.window_start < t1)) or 0
    data = {"month": body.month, "tenant": body.tenant, "project": body.project,
            "summary": {"cost_usd": round(sum(v["cost"] for v in days.values()), 6), "requests": int(sum(v["requests"] for v in days.values())),
                        "tasks": int(sum(v["tasks"] for v in days.values())), "anomalies": n_anom},
            "charts": {"cost_per_task": cpt, "task_success_rate": m.get("task_success_rate"), "guardrail_block_rate": m.get("guardrail_block_rate"),
                       "mat_score": m.get("mat_score"), "mat_stage": m.get("mat_stage")}, "sources": {"mat_error": m.get("error")}}
    return data


@router.post("/reports/monthly", status_code=201)
async def monthly(body: MonthlyIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    data = await build_monthly(body)
    html = render.monthly_html(data)
    with Session(engine()) as s, s.begin():
        row = ReportRow(month=body.month, tenant=body.tenant, project=body.project, data=data, html=html, sent_to=body.send_to)
        s.add(row)
        s.flush()
        rid = row.id
    mail = None
    if body.send_to:
        key = hashlib.sha256(",".join(sorted(body.send_to)).encode()).hexdigest()[:12]
        try:
            rule = await ensure_rule(body.tenant, f"x-rpt-monthly-{key}", {"topic": "plt.rpt.report.ready", "severity": "info",
                                                                            "filter": [{"field": "recipients", "op": "==", "value": key}],
                                                                            "channels": [{"type": "email", "to": body.send_to}]})
            mail = "queued" if rule else "event_only"     # event_only = 조직 관리자가 만든 x-rpt-monthly-<key> 규칙이 메일을 보낸다
        except httpx.HTTPError as exc:
            mail = f"rule error: {exc}"[:200]
        await events.publish(agent, "plt.rpt.report.ready", {"org": body.tenant, "report": rid, "month": body.month, "project": body.project, "recipients": key,
                                                             "url": f"/svc/rpt/v1/reports/{rid}.html", **data["summary"],
                                                             "mat_score": data["charts"]["mat_score"]}, body.tenant)
    return {"id": rid, "month": body.month, "summary": data["summary"], "charts": data["charts"], "html": f"/v1/reports/{rid}.html",
            "pdf": f"/v1/reports/{rid}.pdf", "mail": mail}


def report_row(rid: str) -> ReportRow:
    with Session(engine()) as s:
        r = s.get(ReportRow, rid)
    if r is None:
        raise HTTPException(404, "보고서 없음")
    return r


@router.get("/reports")
async def list_reports(tenant: str = "default"):
    with Session(engine()) as s:
        return {"items": [{"id": r.id, "month": r.month, "project": r.project, "summary": r.data["summary"], "created_at": r.created_at.isoformat()}
                          for r in s.scalars(select(ReportRow).where(ReportRow.tenant == tenant).order_by(ReportRow.created_at.desc()).limit(50))]}


@router.get("/reports/{rid}.html", response_class=HTMLResponse)
async def report_html(rid: str):
    return HTMLResponse(report_row(rid).html)


@router.get("/reports/{rid}.pdf")
async def report_pdf(rid: str):
    d = report_row(rid).data
    c = d["charts"]
    rows = [{"metric": k, "value": c[k]} for k in ("task_success_rate", "guardrail_block_rate", "mat_score")] + \
           [{"metric": f"cost_per_task {p['day']}", "value": p["value"]} for p in c["cost_per_task"]]
    return Response(render.to_pdf(f"ProdAI Atlas monthly report {d['month']} {d['tenant']}/{d['project']} cost ${d['summary']['cost_usd']}", rows,
                                  [(p["day"], p["value"] or 0) for p in c["cost_per_task"]]), media_type="application/pdf")


# ---- 백그라운드 ----

async def worker() -> None:
    """WORM 봉인 · SIEM 전송 · 이상 탐지. 수집은 Manager X-NTF 내장 전달기(ATLAS_RPT_INGEST_URL)가 밀어 넣는다."""
    every = float(os.environ.get("ATLAS_RPT_INTERVAL_S", "10"))
    tick = 0
    while True:
        for job in (seal_worm, flush_sinks):
            try:
                r = job()
                if asyncio.iscoroutine(r):
                    await r
            except Exception as exc:  # noqa: BLE001 — 다음 주기
                log.warning("%s 실패: %s", job.__name__, exc)
        if tick % max(1, int(60 / every)) == 0:
            try:
                await detect(ScanIn())
            except Exception as exc:  # noqa: BLE001
                log.warning("이상 탐지 실패: %s", exc)
        tick += 1
        await asyncio.sleep(every)


async def usage_skill(ctx: TaskContext) -> dict[str, Any]:
    d = ctx.data
    return await usage_report(tenant=ctx.tenant, group_by=d.get("group_by", "project,model"), since=d.get("from"), until=d.get("to"),
                              currency=d.get("currency", "USD"), project=d.get("project"))


agent = AgentSpec.from_file(MANIFEST, description="X-RPT 사용량·감사 리포트 — 계측·비용 그룹핑·차지백·통합 감사(해시 체인·WORM)·SIEM·월간 보고서·이상 탐지",
                            skills=[Skill(id="rpt.usage", name="Usage report", handler=usage_skill,
                                          description="기간·차원별 사용량·비용 집계 (group_by·currency)", tags=["finops", "report"])],
                            routers=[router], background=[worker])
