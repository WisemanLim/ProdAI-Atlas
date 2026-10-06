"""X-SBX Code Sandbox — 에이전트 코드 실행(Python·JavaScript·셸)을 격리 백엔드에서 (FR-X-SBX-01·02·03·06).

  A2A skill sbx.run_code {lang, code, files, timeout_s, limits{cpu_s, memory_mb}, egress{allow[]}, isolation}
  REST: POST /v1/runs · GET /v1/runs[/{id}] · GET /v1/isolation
- 기본 네트워크 차단(내부 전용망). egress.allow 가 있으면 실행마다 egress 프록시 정책을 만들고 끝나면 지운다. 접속 결정은 기록.
- 격리 수준 container·gvisor·microvm 중 백엔드가 제공하는 것만, 테넌트 최소 격리 강제.
- 결과·산출물은 비밀 스캐너(정규식+엔트로피)와 GR-IO(PII) 를 거쳐 돌려준다. 실행은 OTel span(gen_ai.tool.name=sbx.run_code)·실행 기록으로 감사.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import secrets as pysecrets
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from opentelemetry import trace
from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from atlas_agent_core import (
    AgentSpec,
    InputRequired,
    Skill,
    TaskContext,
    events,
    otel,
    secrets,
)
from atlas_agent_core.tasks import Base

MANIFEST = Path(__file__).with_name("atlas-agent.json")
router = APIRouter(prefix="/v1", tags=["sandbox"])
TRANSPORT: httpx.AsyncBaseTransport | None = None
LEVEL = {"container": 1, "gvisor": 2, "microvm": 3}


class RunRow(Base):
    __tablename__ = "sbx_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    lang: Mapped[str] = mapped_column(String(16))
    code_sha256: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))      # ok · error · limit_exceeded
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    isolation: Mapped[str] = mapped_column(String(16))
    limits: Mapped[dict] = mapped_column(JSON)
    egress: Mapped[dict] = mapped_column(JSON)            # allow · decisions[]
    findings: Mapped[list] = mapped_column(JSON)          # 산출물 검사 결과
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def cfg() -> dict[str, Any]:
    return agent.manifest["spec"]["config"]


def url(env: str, default: str) -> str:
    return (os.environ.get(env) or default).rstrip("/")


# ---- 산출물 검사 (X-SBX-06) ----
SECRET_RE = [("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")), ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
             ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
             ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")), ("atlas_key", re.compile(r"\batl_[a-z]+_[A-Za-z0-9_]{20,}\b"))]
TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{32,}\b")


def entropy(s: str) -> float:
    c = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in c.values())


def scan_secrets(text: str) -> tuple[str, list[dict[str, Any]]]:
    found = []
    for name, rx in SECRET_RE:
        for m in rx.finditer(text):
            found.append({"type": name, "sha256": hashlib.sha256(m.group(0).encode()).hexdigest()[:16]})
        text = rx.sub(f"[REDACTED:{name}]", text)
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        if entropy(tok) >= 4.0 and not tok.startswith("REDACTED"):
            found.append({"type": "high_entropy", "sha256": hashlib.sha256(tok.encode()).hexdigest()[:16]})
            text = text.replace(tok, "[REDACTED:high_entropy]")
    return text, found


async def pii(tenant: str, texts: list[str]) -> tuple[list[str], list[str]]:
    if not texts:
        return texts, []
    try:
        async with httpx.AsyncClient(timeout=10, transport=TRANSPORT) as c:
            r = await c.post(url("ATLAS_GUARD_URL", "http://agent-gr:8000") + "/v1/guard/check", json={"tenant": tenant, "boundary": "output", "contents": texts},
                             headers=otel.inject({}))
        r.raise_for_status()
        v = r.json()
    except (httpx.HTTPError, ValueError):
        return texts, []
    if v.get("decision") != "transform":
        return texts, []
    return (v.get("contents") or texts), list(v.get("categories") or [])


class Limits(BaseModel):
    cpu_s: int | None = Field(None, ge=1)
    memory_mb: int | None = Field(None, ge=64)


class Egress(BaseModel):
    allow: list[str] = []


class RunIn(BaseModel):
    lang: str = Field("python", pattern="^(python|javascript|shell)$")
    code: str = Field(min_length=1, max_length=200_000)
    files: dict[str, str] = {}
    timeout_s: int | None = Field(None, ge=1)
    limits: Limits = Limits()
    egress: Egress = Egress()
    isolation: str | None = Field(None, pattern="^(container|gvisor|microvm)$")
    tenant: str = "default"


def resolve_limits(body: RunIn) -> dict[str, int]:
    d, mx = cfg()["limits"], cfg()["maxLimits"]
    out = {"timeout_s": body.timeout_s or d["timeout_s"], "cpu_s": body.limits.cpu_s or d["cpu_s"], "memory_mb": body.limits.memory_mb or d["memory_mb"]}
    over = [k for k, v in out.items() if v > mx[k]]
    if over:
        raise HTTPException(422, f"상한 초과: {over} (최대 {mx})")
    return out


def resolve_isolation(body: RunIn) -> str:
    """격리 수준 (FR-X-SBX-03): 요청 수준과 테넌트 최소 수준 중 높은 것, 백엔드가 제공하지 않으면 거부."""
    avail = cfg()["isolations"]
    need = max([body.isolation or "container", cfg()["minIsolation"].get(body.tenant, cfg()["minIsolation"].get("default", "container"))], key=LEVEL.get)
    if need not in avail:
        raise HTTPException(422, f"격리 수준 {need} 를 이 백엔드에서 제공하지 않음 (사용 가능: {avail}) — gVisor(runsc)·Firecracker 런타임 필요")
    return need


async def execute(body: RunIn) -> dict[str, Any]:
    lim, iso = resolve_limits(body), resolve_isolation(body)
    rid = str(uuid.uuid4())
    sbx, proxy = url("ATLAS_SANDBOX_URL", "http://sandbox:8000"), url("ATLAS_EGRESS_ADMIN_URL", "http://egress-proxy:8080")
    req: dict[str, Any] = {"code": body.code, "lang": body.lang, "files": body.files, "timeout_s": lim["timeout_s"], "mem_mb": lim["memory_mb"], "cpu_s": lim["cpu_s"]}
    decisions: list[dict[str, Any]] = []
    tracer = trace.get_tracer("atlas.sbx")
    with tracer.start_as_current_span("execute_tool sbx.run_code", attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "sbx.run_code",
                                                                              "atlas.sbx.lang": body.lang, "atlas.sbx.isolation": iso, "atlas.tenant": body.tenant}) as span:
        async with httpx.AsyncClient(timeout=lim["timeout_s"] + 30, transport=TRANSPORT) as c:
            admin = None
            if body.egress.allow:
                admin = {"X-Egress-Admin": await secrets.get("egress-admin-token", env="ATLAS_EGRESS_ADMIN_TOKEN", dev_default="local-egress-admin")}
                tok = pysecrets.token_urlsafe(16)
                r = await c.put(f"{proxy}/policies/{rid}", headers=admin, json={"token": tok, "allow": body.egress.allow, "ttl_s": lim["timeout_s"] + 30})
                r.raise_for_status()
                req["egress"] = {"proxy": url("ATLAS_EGRESS_PROXY_URL", "http://egress-proxy:3128"), "policy": rid, "token": tok}
            try:
                token = await secrets.get("sandbox-token", env="ATLAS_SANDBOX_TOKEN", dev_default="local-dev-sandbox")
                res = await c.post(f"{sbx}/run", json=req, headers={"X-Sandbox-Token": token})
                if res.status_code == 401:
                    token = await secrets.get("sandbox-token", env="ATLAS_SANDBOX_TOKEN", dev_default="local-dev-sandbox", fresh=True)
                    res = await c.post(f"{sbx}/run", json=req, headers={"X-Sandbox-Token": token})
                res.raise_for_status()
                run = res.json()
            finally:
                if admin:
                    decisions = (await c.get(f"{proxy}/log", params={"policy": rid}, headers=admin)).json().get("items", [])
                    await c.delete(f"{proxy}/policies/{rid}", headers=admin)
        if run.get("error"):
            raise HTTPException(422, run["error"])
        # 산출물 검사: 비밀 → PII
        findings: list[dict[str, Any]] = []
        texts = [run.get("stdout", ""), run.get("stderr", "")] + [a["text"] for a in run.get("artifacts", [])]
        clean = []
        for t in texts:
            t2, f = scan_secrets(t)
            clean.append(t2)
            findings += f
        clean, pcats = await pii(body.tenant, clean)
        findings += [{"type": f"pii:{c}"} for c in pcats]
        run["stdout"], run["stderr"] = clean[0], clean[1]
        for a, t in zip(run.get("artifacts", []), clean[2:]):
            a["text"] = t
        limit_hit = bool(run.get("timed_out") or run.get("cpu_exceeded") or "MemoryError" in run.get("stderr", ""))
        status = "limit_exceeded" if limit_hit else ("ok" if run.get("exit_code") == 0 else "error")
        span.set_attribute("atlas.sbx.exit_code", run.get("exit_code") if run.get("exit_code") is not None else -1)
        span.set_attribute("atlas.sbx.status", status)
        span.set_attribute("atlas.sbx.egress_denied", sum(1 for d in decisions if d["decision"] == "deny"))
    egress = {"allow": body.egress.allow, "decisions": [{k: d[k] for k in ("host", "port", "decision", "reason")} for d in decisions]}
    with Session(agent.resources["engine"]) as s, s.begin():
        s.add(RunRow(id=rid, tenant=body.tenant, lang=body.lang, code_sha256=hashlib.sha256(body.code.encode()).hexdigest(), code=body.code[:65536], status=status,
                     exit_code=run.get("exit_code"), isolation=iso, limits=lim, egress=egress, findings=findings, duration_ms=int(run.get("duration_ms") or 0),
                     created_at=datetime.now(UTC)))
    await events.publish(agent, "plt.sbx.run.completed", {"run": rid, "lang": body.lang, "status": status, "duration_ms": int(run.get("duration_ms") or 0),
                                                          "cpu_s_limit": lim["cpu_s"], "egress": bool(body.egress.allow)}, body.tenant)   # 사용량 계측 (X-RPT-01 sbx_cpu_s)
    if limit_hit:
        await events.publish(agent, "plt.sbx.limit_exceeded", {"run": rid, "limits": lim, "timed_out": run.get("timed_out"), "cpu_exceeded": run.get("cpu_exceeded")}, body.tenant)
    return {"run_id": rid, "status": status, "exit_code": run.get("exit_code"), "stdout": run["stdout"], "stderr": run["stderr"], "artifacts": run.get("artifacts", []),
            "timed_out": run.get("timed_out"), "isolation": iso, "limits": lim, "egress": egress, "findings": findings, "duration_ms": run.get("duration_ms")}


@router.post("/runs")
async def create_run(body: RunIn):
    return await execute(body)


def view(r: RunRow) -> dict[str, Any]:
    return {"run_id": r.id, "lang": r.lang, "status": r.status, "exit_code": r.exit_code, "isolation": r.isolation, "limits": r.limits, "egress": r.egress,
            "findings": r.findings, "duration_ms": r.duration_ms, "code_sha256": r.code_sha256, "created_at": r.created_at.isoformat()}


@router.get("/runs")
async def list_runs(tenant: str = "default", limit: int = 50):
    with Session(agent.resources["engine"]) as s:
        rows = list(s.scalars(select(RunRow).where(RunRow.tenant == tenant).order_by(RunRow.created_at.desc()).limit(min(limit, 500))))
    return {"items": [view(r) for r in rows]}


@router.get("/runs/{rid}")
async def get_run(rid: str):
    with Session(agent.resources["engine"]) as s:
        r = s.get(RunRow, rid)
    if r is None:
        raise HTTPException(404, "run not found")
    return {**view(r), "code": r.code}


@router.get("/isolation")
async def isolation():
    return {"available": cfg()["isolations"], "min": cfg()["minIsolation"], "backend": cfg()["backend"], "levels": list(LEVEL)}


async def run_skill(ctx: TaskContext) -> dict[str, Any]:
    if not ctx.data.get("code"):
        raise InputRequired("data.code(·lang·files·timeout_s·limits·egress) 가 필요합니다")
    try:
        return await execute(RunIn(**{**ctx.data, "tenant": ctx.tenant}))
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc


agent = AgentSpec.from_file(MANIFEST, description="X-SBX 코드 샌드박스 — Python·JavaScript·셸 격리 실행(자원·송신 상한·격리 수준·실행 감사·산출물 검사)",
                            skills=[Skill(id="sbx.run_code", name="Run code", handler=run_skill,
                                          description="코드를 격리 샌드박스에서 실행 → stdout·stderr·exit_code·artifacts (기본 네트워크 차단)", tags=["sandbox", "code"])],
                            routers=[router])
