"""GR-IO Input/Output Safety — 경계별 가드레일 파이프라인 서비스 (FR-GR-IO-01·02·04·06·08, GR-PII·JB·POL 모듈 합성).

REST (Gateway pre/post 훅·AL-RUN 도구 경계가 호출):
  POST /v1/guard/check · GET /v1/guard/verdicts · POST /v1/guard/verdicts/{id}/feedback
  POST /v1/guard/exceptions · GET|PUT /v1/guard/policies/{tenant}   (변경 API 는 Bearer ATLAS_GUARD_ADMIN_TOKEN)
  POST /v1/guard/reveal (역마스킹, 권한 pii:reveal — GR-PII-06) · POST /v1/guard/tune (섀도 판정 PR 곡선·임계값 제안 — GR-IO-09)
A2A skill: guardrail.check
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from atlas_agent_core import (
    AgentSpec,
    InputRequired,
    Skill,
    TaskContext,
    events,
    secrets,
)
from units import gr_jb, gr_pii, gr_pol
from units.gr_io import pipeline
from units.gr_io.checkers import session_key
from units.gr_io.store import GuardStore, VaultRow, now, vault_load, vault_put

MANIFEST = Path(__file__).with_name("atlas-agent.json")
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def store() -> GuardStore:
    return GuardStore(agent.resources["engine"])


def bundle_for(tenant: str) -> dict[str, Any]:
    """테넌트 정책 번들(최신 게시본) — 없으면 baseline-v1 자동 적용 (FR-GR-POL-07). 10초 캐시."""
    hit = _CACHE.get(tenant)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    row = store().latest(tenant)
    b = row.bundle if row else gr_pol.baseline()
    _CACHE[tenant] = (time.monotonic(), b)
    return b


def require_admin(authorization: str | None) -> str:
    token = os.environ.get("ATLAS_GUARD_ADMIN_TOKEN", "")
    if not token or token == "CHANGE_ME" or authorization != f"Bearer {token}":
        raise HTTPException(401, "관리 토큰 필요")
    return "admin"


def vault_key(secret: str, tenant: str) -> bytes:
    return hashlib.sha256(f"atlas-guard-vault|{secret}|{tenant}".encode()).digest()


class VaultPseudonymizer(gr_pii.Pseudonymizer):
    """세션 가명을 볼트에 남긴다 — 다음 요청에서도 같은 원문 = 같은 토큰, 원문은 AES-GCM 암호문만 (FR-GR-PII-06)."""

    def __init__(self, skey: bytes, tenant: str, session: str, secret: str, ttl_s: int):
        super().__init__(skey)
        self.tenant, self.session, self.aes, self.ttl = tenant, session, AESGCM(vault_key(secret, tenant)), ttl_s
        for r in vault_load(engine(), tenant, session):
            self.tokens[r.value_hmac] = r.token
            n = int(re.search(r"_(\d+)>$", r.token).group(1))
            self.counts[r.entity_type] = max(self.counts.get(r.entity_type, 0), n)

    def token(self, etype: str, value: str) -> str:
        h = hmac.new(self.key, f"{etype}|{value}".encode(), hashlib.sha256).hexdigest()
        known = h in self.tokens
        tok = super().token(etype, value)
        if not known:
            nonce = os.urandom(12)
            ct = base64.b64encode(nonce + self.aes.encrypt(nonce, value.encode(), f"{self.tenant}|{self.session}|{tok}".encode())).decode()
            vault_put(engine(), VaultRow(tenant=self.tenant, session=self.session, token=tok, entity_type=etype, value_hmac=h, ciphertext=ct,
                                         expires_at=now() + timedelta(seconds=self.ttl)))
        return tok


def engine():
    return agent.resources["engine"]


class CheckContext(BaseModel):
    tool: dict[str, Any] | None = None             # tool_call: {name, side_effect, input_schema}
    args: dict[str, Any] | None = None
    provenance: dict[str, str] | None = None       # 인자 필드 → trusted|untrusted
    response_schema: dict[str, Any] | None = None  # output: JSON Schema
    sink: str | None = Field(None, pattern="^(html|sql|shell)$")
    session: str | None = None                     # 일관 가명 범위
    principal: dict[str, Any] | None = None


class CheckRequest(BaseModel):
    tenant: str = "default"
    boundary: str
    contents: list[str] = []
    context: CheckContext = CheckContext()


async def check(req: CheckRequest) -> dict[str, Any]:
    bundle = bundle_for(req.tenant)
    contents = req.contents or [""]
    # 가명·해시 키 — Manager 비밀 중개(guard-secret), 기동 직후엔 즉시 조회. env 는 허용 시(단독 개발)만 (FR-PLT-SEC-03)
    key = await secrets.get("guard-secret", env="ATLAS_GUARD_SECRET", dev_default="atlas-dev-guard-secret")
    salt = f"{key}|{req.tenant}"
    skey = session_key(key, req.tenant, req.context.session or req.tenant)
    pseudo = (VaultPseudonymizer(skey, req.tenant, req.context.session, key, int(bundle.get("pii", {}).get("vault_ttl_s", 86400)))
              if req.context.session else gr_pii.Pseudonymizer(skey))      # 세션을 밝힌 요청만 볼트 (역마스킹 대상)
    ctx = {**req.context.model_dump(), "bundle": bundle, "pii": bundle.get("pii", {}), "salt": salt, "pseudo": pseudo}
    payload = json.dumps({"c": contents, "tool": req.context.tool, "args": req.context.args}, sort_keys=True, ensure_ascii=False)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    v = await pipeline.run(bundle, req.boundary, contents, ctx, excepted=store().excepted(req.tenant, sha))
    vid = store().add_verdict(req.tenant, req.boundary, v.decision, sha, v.categories, v.stages, bundle.get("version", "?"))
    body = {"verdict_id": vid, "decision": v.decision, "reasons": v.reasons, "categories": v.categories, "stages": v.stages,
            "latency_ms": v.latency_ms, "policy_version": bundle.get("version")}
    if v.decision == pipeline.TRANSFORM:
        body["contents"] = v.contents
    if v.spotlight_rule:
        body["spotlight_rule"] = v.spotlight_rule
    leaked = sorted({c for c in v.categories if c.startswith(gr_pii.SECRET_PREFIX)})
    if leaked:                                       # 비밀 노출 → 키 폐기 권고 이벤트 (GR-PII-08)
        await events.publish(agent, "plt.gr.secret_detected", {"tenant": req.tenant, "boundary": req.boundary, "types": leaked, "verdict": vid,
                                                               "advice": "노출된 자격증명을 폐기·회전하세요 (POST /api/v1/orgs/{o}/secrets/{name}/rotate)"},
                             req.tenant)
    return body


router = APIRouter(prefix="/v1/guard", tags=["guard"])


@router.post("/check")
async def check_route(req: CheckRequest):
    if req.boundary not in pipeline.BOUNDARIES:
        raise HTTPException(422, f"boundary: {', '.join(pipeline.BOUNDARIES)}")
    return await check(req)


@router.get("/verdicts")
async def list_verdicts(tenant: str = "default", limit: int = 50):
    return {"items": [{"id": r.id, "boundary": r.boundary, "decision": r.decision, "categories": r.categories,
                       "policy_version": r.policy_version, "created_at": r.created_at.isoformat()} for r in store().verdicts(tenant, min(limit, 200))]}


class Feedback(BaseModel):
    kind: str = Field(pattern="^(false_positive|false_negative)$")
    note: str | None = None


@router.post("/verdicts/{vid}/feedback", status_code=201)
async def feedback(vid: str, body: Feedback):
    if store().verdict(vid) is None:
        raise HTTPException(404, "verdict not found")
    return {"id": store().add_feedback(vid, body.kind, body.note), "verdict_id": vid, "status": "queued"}


class ExceptionReq(BaseModel):
    verdict_id: str
    checker: str
    ttl_days: int = Field(30, ge=1, le=365)


@router.post("/exceptions", status_code=201)
async def add_exception(body: ExceptionReq, authorization: str | None = Header(None)):
    """오탐 승인 → 같은 내용(해시)에 대해 해당 검사기를 만료일까지 건너뛴다."""
    require_admin(authorization)
    v = store().verdict(body.verdict_id)
    if v is None:
        raise HTTPException(404, "verdict not found")
    return {"id": store().add_exception(v.tenant, body.checker, v.content_sha256, body.ttl_days), "tenant": v.tenant, "checker": body.checker}


@router.get("/policies/{tenant}")
async def get_policy(tenant: str, response: Response):
    row = store().latest(tenant)
    b = row.bundle if row else gr_pol.baseline()
    response.headers["ETag"] = f'"{b.get("version")}"'
    return {"tenant": tenant, "source": "published" if row else "baseline", "bundle": b}


@router.put("/policies/{tenant}", status_code=201)
async def put_policy(tenant: str, bundle: dict[str, Any], authorization: str | None = Header(None)):
    who = require_admin(authorization)
    try:
        gr_pol.validate_bundle(bundle)
    except gr_pol.PolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not store().publish(tenant, bundle, who):
        raise HTTPException(409, f"version {bundle['version']} 이미 게시됨 — 새 버전 필요")
    _CACHE.pop(tenant, None)
    return {"tenant": tenant, "version": bundle["version"]}


# ---- 역마스킹 (GR-PII-06) ----

class RevealRequest(BaseModel):
    tenant: str = "default"
    session: str
    contents: list[str]
    principal: dict[str, Any] = {}


TOKEN_RE = re.compile(r"<\s*([A-Z_]+?)\s*[_ ]\s*(\d+)\s*>")


@router.post("/reveal")
async def reveal(req: RevealRequest):
    """출력 경계에서 가명 → 원문 복원. 권한 pii:reveal 이 없으면 그대로(마스킹 유지). LLM 이 토큰을 띄어 쓴 변형도 찾는다."""
    perms = set(req.principal.get("permissions") or [])
    rows = {r.token: r for r in vault_load(engine(), req.tenant, req.session)}
    key = await secrets.get("guard-secret", env="ATLAS_GUARD_SECRET", dev_default="atlas-dev-guard-secret")
    found: list[str] = []

    def sub(m: re.Match) -> str:
        tok = f"<{m.group(1)}_{int(m.group(2))}>"
        r = rows.get(tok)
        if r is None or r.entity_type.startswith(gr_pii.SECRET_PREFIX):
            return m.group(0)
        found.append(tok)
        if "pii:reveal" not in perms:
            return m.group(0)
        raw = base64.b64decode(r.ciphertext)
        return AESGCM(vault_key(key, req.tenant)).decrypt(raw[:12], raw[12:], f"{req.tenant}|{req.session}|{r.token}".encode()).decode()

    out = [TOKEN_RE.sub(sub, c) for c in req.contents]
    revealed = "pii:reveal" in perms and bool(found)
    vid = store().add_verdict(req.tenant, "output", "reveal" if revealed else "reveal_denied", hashlib.sha256(json.dumps(req.contents).encode()).hexdigest(),
                              sorted({t.strip("<>").rsplit("_", 1)[0] for t in found}), [{"checker": "reveal", "principal": req.principal.get("sub"),
                                                                                        "tokens": len(found), "allowed": "pii:reveal" in perms}], "reveal")
    return {"contents": out, "revealed": revealed, "tokens": len(found), "verdict_id": vid,
            **({} if "pii:reveal" in perms else {"reason": "권한 pii:reveal 없음 — 마스킹 유지"})}


# ---- 임계값 튜닝 (GR-IO-09) ----

class TuneItem(BaseModel):
    text: str
    label: bool                              # True = 차단해야 하는 공격


class TuneRequest(BaseModel):
    checker: str = Field("jailbreak", pattern="^(jailbreak|indirect)$")
    items: list[TuneItem] = Field(min_length=2)
    max_fpr: float = Field(0.05, ge=0, le=1)
    min_recall: float = Field(0.0, ge=0, le=1)
    tenant: str = "default"


@router.post("/tune")
async def tune(req: TuneRequest):
    """라벨 셋으로 점수 분포 → 임계값별 정밀도·재현율·FPR → 목표(max_fpr·min_recall) 를 만족하는 최대 재현율 임계값 제안. 적용은 정책 새 버전 게시로만."""
    score = (lambda t: gr_jb.classify(t).score) if req.checker == "jailbreak" else (lambda t: gr_jb.scan_indirect(t).score)
    scored = [(score(i.text), i.label) for i in req.items]
    pos, neg = sum(1 for _, y in scored if y), sum(1 for _, y in scored if not y)
    curve = []
    for th in sorted({round(s, 3) for s, _ in scored} | {0.6}):
        tp = sum(1 for s, y in scored if y and s >= th)
        fp = sum(1 for s, y in scored if not y and s >= th)
        curve.append({"threshold": th, "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0, "recall": round(tp / pos, 4) if pos else 0.0,
                      "fpr": round(fp / neg, 4) if neg else 0.0})
    ok = [p for p in curve if p["fpr"] <= req.max_fpr and p["recall"] >= req.min_recall and p["threshold"] > 0]
    best = max(ok, key=lambda p: (p["recall"], p["precision"], -p["threshold"]), default=None)
    cur = next((st.get("threshold", 0.6) for b in bundle_for(req.tenant)["boundaries"].values() for st in b if st["checker"] == "jailbreak"), 0.6)
    return {"checker": req.checker, "items": len(scored), "positives": pos, "negatives": neg, "curve": curve, "current_threshold": cur,
            "suggested": best, "promote": "새 정책 버전(PUT /v1/guard/policies/{tenant})으로 임계값을 바꾸고, 먼저 mode: shadow 로 운영 데이터에서 확인"}


async def skill(ctx: TaskContext) -> dict[str, Any]:
    if "boundary" not in ctx.data:
        raise InputRequired("boundary·contents 가 필요합니다")
    return await check(CheckRequest(tenant=ctx.tenant, **ctx.data))


agent = AgentSpec.from_file(MANIFEST, description="GR-IO — 입력·출력·도구·검색 경계마다 PII·탈옥·유해성·형식·정책 검사기 체인을 실행한다.",
                            skills=[Skill(id="guardrail.check", name="Guardrail check", handler=skill,
                                          description="경계별 가드레일 판정 allow|block|transform|require_approval")],
                            routers=[router])
