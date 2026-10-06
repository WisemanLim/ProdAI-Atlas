"""X-MKT Templates & Marketplace (FR-X-MKT-01~06).

- 카탈로그 (MKT-01): atlas-template.yaml {kind, name, version(semver), unit_ids[], requires[], params(JSON Schema), payload, sbom, eval?}.
  번들 템플릿(catalog/*.yaml)은 기동 시 플랫폼 키로 서명·provenance 를 붙여 public 으로 게시. 인덱스는 메타 DB(OCI/ORAS 아티팩트는 후속).
- 원클릭 설치 (MKT-02): params 스키마 → 콘솔 폼 · 검증 · {{params.x}} 치환 → kind 별 적용
  (agent = Manager 등록 + 배포 요청 · mcp-server = MCP-REG 등록 · 그 외 = 산출물 전달). 비밀 파라미터(x-atlas-secret)는 Manager 비밀로만.
- 서명·출처 (MKT-03): ed25519 서명 + SLSA provenance + SBOM 필수(mkt.require_signature), 취약 구성요소·악성 지시(정적 스캔) 차단.
- 사내 게시 (MKT-04): 가시성 private/org/public · 게시 = X-HITL 승인(Manager 승인함) → 승인되면 플랫폼 키로 서명 · 폐기·대체 링크.
- 업데이트 (MKT-05): 설치본 vs 최신 semver · 변경 로그 · plt.mkt.update_available · 업그레이드 전 EV 회귀(mkt.upgrade.require_eval_pass).
- MCP 카탈로그 (MKT-06): 공개 MCP Registry API 동기화(읽기) → 신뢰 수준·권한 검토 필수 → MCP-REG 등록·승인.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import yaml
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from atlas_agent_core import (
    AgentSpec,
    InputRequired,
    Skill,
    TaskContext,
    events,
    otel,
    secrets,
)
from atlas_agent_core.a2a_client import TokenProvider
from units.mcp_reg.scan import scan
from units.mkt import signing
from units.mkt.store import InstallRow, KeyRow, McpCatalogRow, TemplateRow

MANIFEST = Path(__file__).with_name("atlas-agent.json")
CATALOG = Path(__file__).with_name("catalog")
TRANSPORT: httpx.AsyncBaseTransport | None = None
STATE: dict[str, Any] = {"key": None}
KINDS = ("agent", "workflow", "mcp-server", "guardrail-policy", "eval-dataset", "dashboard", "prompt", "skill")
router = APIRouter(prefix="/v1", tags=["marketplace"])
admin_router = APIRouter(prefix="/admin", tags=["marketplace-admin"])
log = logging.getLogger("atlas.mkt")


def cfg() -> dict[str, Any]:
    return agent.manifest["spec"]["config"]


def url(env: str, default: str) -> str:
    return (os.environ.get(env) or default).rstrip("/")


def engine():
    return agent.resources["engine"]


def semver(v: str) -> tuple[int, ...]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


async def require_admin(authorization: str | None) -> None:
    tok = await secrets.get("mkt-admin-token", env="ATLAS_MKT_ADMIN_TOKEN", dev_default="local-mkt-admin")
    if not tok or "CHANGE_ME" in tok or not hmac.compare_digest(authorization or "", f"Bearer {tok}"):
        raise HTTPException(401, "관리 토큰 필요")


# ---- 키·서명 ----

def platform_key():
    if STATE["key"] is None:
        STATE["key"] = signing.load_or_create(Path(os.environ.get("ATLAS_MKT_KEY_PATH", "data/mkt/platform-ed25519.pem")))
        pem = signing.pub_pem(STATE["key"].public_key())
        with Session(engine()) as s, s.begin():
            if s.get(KeyRow, signing.kid_of(pem)) is None:
                s.add(KeyRow(kid=signing.kid_of(pem), public_key=pem, owner="platform", source="platform"))
    return STATE["key"]


def trusted_keys() -> dict[str, str]:
    with Session(engine()) as s:
        return {k.kid: k.public_key for k in s.scalars(select(KeyRow))}


def verify_row(t: TemplateRow) -> dict[str, Any]:
    v = signing.verify(t.digest, t.signature, t.provenance, t.sbom, trusted_keys(), cfg()["trustedBuilders"], cfg()["blockedComponents"],
                       bool(cfg()["requireSignature"]))
    if signing.bundle_digest(t.manifest, t.files) != t.digest:
        v["ok"], v["reasons"] = False, v["reasons"] + ["번들 digest 불일치 (저장 후 변조)"]
    if (t.scan or {}).get("score", 0) >= int(cfg()["scanBlockScore"]):
        v["ok"], v["reasons"] = False, v["reasons"] + [f"정적 스캔 점수 {t.scan['score']} — 악성 지시 의심"]
    return v


def static_scan(manifest: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
    items = [{"name": f"text{i}", "description": t} for i, t in enumerate(signing.texts(manifest.get("payload")) + list(files.values()))]
    r = scan(items)
    return {"score": r["score"], "rules": sorted({f["rule"] for fs in r["findings"].values() for f in fs})}


def sign_row(t: TemplateRow, source: str) -> None:
    t.provenance = signing.statement(t.name, t.version, t.digest, signing.PLATFORM_BUILDER, source)
    t.signature = signing.sign(platform_key(), t.digest, t.provenance, t.sbom)


# ---- 카탈로그 (MKT-01) ----

def parse_manifest(raw: str | dict[str, Any]) -> dict[str, Any]:
    m = yaml.safe_load(raw) if isinstance(raw, str) else raw
    if not isinstance(m, dict):
        raise HTTPException(422, "atlas-template.yaml 이 객체가 아님")
    miss = [k for k in ("kind", "name", "version", "title", "params", "payload") if k not in m]
    if miss:
        raise HTTPException(422, f"필수 필드 없음: {miss}")
    if m["kind"] not in KINDS:
        raise HTTPException(422, f"kind = {KINDS}")
    if not re.match(r"^[a-z][a-z0-9-]{1,80}$", str(m["name"])) or semver(str(m["version"])) == (0, 0, 0):
        raise HTTPException(422, "name = 소문자 slug · version = semver")
    try:
        jsonschema.Draft202012Validator.check_schema(m["params"])
    except jsonschema.SchemaError as exc:
        raise HTTPException(422, f"params JSON Schema 오류: {exc.message}") from exc
    m["version"] = str(m["version"])
    return m


def sync_bundled() -> int:
    """번들 카탈로그 → DB (없는 버전만). 1st-party 라 플랫폼 키로 서명·provenance 생성."""
    n = 0
    with Session(engine()) as s, s.begin():
        for p in sorted(CATALOG.glob("*.yaml")):
            m = parse_manifest(p.read_text(encoding="utf-8"))
            if s.scalar(select(TemplateRow).where(TemplateRow.name == m["name"], TemplateRow.version == m["version"])):
                continue
            t = new_row(m, {}, visibility="public", tenant="*", project="*", publisher="platform")
            t.status, t.published_at = "published", datetime.now(UTC)
            sign_row(t, "bundled")
            s.add(t)
            n += 1
    return n


def new_row(m: dict[str, Any], files: dict[str, str], **kw: Any) -> TemplateRow:
    return TemplateRow(name=m["name"], version=m["version"], kind=m["kind"], title=m["title"], description=m.get("description", ""),
                       unit_ids=m.get("unit_ids", []), layer=m.get("layer", ""), manifest=m, files=files, digest=signing.bundle_digest(m, files),
                       sbom=m.get("sbom"), scan=static_scan(m, files), **kw)


def visible(t: TemplateRow, tenant: str, project: str) -> bool:
    return t.visibility == "public" or (t.tenant == tenant and (t.visibility == "org" or t.project == project))


def card(t: TemplateRow, installs: int = 0) -> dict[str, Any]:
    return {"name": t.name, "version": t.version, "kind": t.kind, "title": t.title, "description": t.description, "unit_ids": t.unit_ids,
            "layer": t.layer, "visibility": t.visibility, "status": t.status, "publisher": t.publisher, "replaced_by": t.replaced_by,
            "requires": t.manifest.get("requires", []), "changelog": t.manifest.get("changelog", ""), "digest": t.digest,
            "signed": bool(t.signature), "installs": installs, "published_at": t.published_at.isoformat() if t.published_at else None}


def rows(name: str | None = None) -> list[TemplateRow]:
    with Session(engine()) as s:
        q = select(TemplateRow)
        if name:
            q = q.where(TemplateRow.name == name)
        return list(s.scalars(q))


def latest(name: str, tenant: str, project: str) -> TemplateRow | None:
    pub = [t for t in rows(name) if t.status == "published" and visible(t, tenant, project)]
    return max(pub, key=lambda t: semver(t.version), default=None)


def get_row(name: str, version: str) -> TemplateRow:
    with Session(engine()) as s:
        t = s.scalar(select(TemplateRow).where(TemplateRow.name == name, TemplateRow.version == version))
    if t is None:
        raise HTTPException(404, f"템플릿 {name}@{version} 없음")
    return t


@router.get("/templates")
async def list_templates(tenant: str = "default", project: str = "default", kind: str | None = None, layer: str | None = None,
                         unit_id: str | None = None, q: str | None = None, all_versions: bool = False, include_drafts: bool = False):
    with Session(engine()) as s:
        counts: dict[str, int] = {}
        for i in s.scalars(select(InstallRow).where(InstallRow.tenant == tenant)):
            counts[i.template] = counts.get(i.template, 0) + 1
    out: dict[str, TemplateRow] = {}
    items = []
    for t in rows():
        if not visible(t, tenant, project) or (t.status not in ("published", "deprecated") and not include_drafts):
            continue
        if (kind and t.kind != kind) or (layer and t.layer != layer) or (unit_id and unit_id not in t.unit_ids):
            continue
        if q and q.lower() not in f"{t.name} {t.title} {t.description}".lower():
            continue
        if all_versions:
            items.append(t)
        elif t.name not in out or (t.status == "published", semver(t.version)) > (out[t.name].status == "published", semver(out[t.name].version)):
            out[t.name] = t                                  # 대표 버전 = 최신 게시본 (폐기본은 게시본이 없을 때만)
    items = items if all_versions else list(out.values())
    return {"items": [card(t, counts.get(t.name, 0)) for t in sorted(items, key=lambda t: (t.name, semver(t.version)))],
            "facets": {"kinds": sorted({t.kind for t in items}), "layers": sorted({t.layer for t in items if t.layer})}}


@router.get("/templates/{name}/versions/{version}")
async def get_template(name: str, version: str, tenant: str = "default", project: str = "default"):
    t = get_row(name, version)
    if not visible(t, tenant, project):
        raise HTTPException(404, "템플릿 없음")
    return {**card(t), "params": t.manifest["params"], "manifest": t.manifest, "verification": verify_row(t), "scan": t.scan,
            "signature": t.signature, "provenance": t.provenance, "sbom": t.sbom, "versions": sorted((x.version for x in rows(name)), key=semver)}


@router.get("/templates/{name}/versions/{version}/form")
async def form(name: str, version: str):
    """설치 폼 스키마 (MKT-02) — JSON Schema + UI 힌트(비밀은 password 위젯)."""
    sch = get_row(name, version).manifest["params"]
    ui = {k: ({"ui:widget": "password"} if v.get("x-atlas-secret") or v.get("format") == "password" else {}) for k, v in sch.get("properties", {}).items()}
    return {"schema": sch, "uiSchema": ui}


# ---- 사내 게시 (MKT-04) ----

class UploadIn(BaseModel):
    manifest: str | dict[str, Any]
    files: dict[str, str] = {}
    visibility: str = Field("private", pattern="^(private|org|public)$")
    tenant: str = "default"
    project: str = "default"
    publisher: str = "user"
    signature: dict[str, Any] | None = None       # 외부 게시자 서명본 (kid · sig)
    provenance: dict[str, Any] | None = None


@router.post("/templates", status_code=201)
async def upload(body: UploadIn):
    m = parse_manifest(body.manifest)
    with Session(engine()) as s, s.begin():
        if s.scalar(select(TemplateRow).where(TemplateRow.name == m["name"], TemplateRow.version == m["version"])):
            raise HTTPException(409, f"{m['name']}@{m['version']} 이미 있음 — 버전을 올리세요")
        older = [t for t in s.scalars(select(TemplateRow).where(TemplateRow.name == m["name"]))]
        if older and any(t.tenant not in (body.tenant, "*") for t in older):
            raise HTTPException(403, "다른 조직이 소유한 템플릿 이름")
        if body.visibility == "public" and body.tenant != "default":
            raise HTTPException(403, "public 게시는 플랫폼 조직만")
        t = new_row(m, body.files, visibility=body.visibility, tenant=body.tenant, project=body.project, publisher=body.publisher,
                    signature=body.signature, provenance=body.provenance)
        s.add(t)
        s.flush()
        out = {**card(t), "scan": t.scan}
        has_sig = bool(body.signature)
    return {**out, "verification": verify_row(get_row(m["name"], m["version"])) if has_sig else None}


async def manager_token() -> str | None:
    mgr = url("ATLAS_MANAGER_URL", "")
    if not mgr:
        return None
    return await TokenProvider(mgr, os.environ.get("ATLAS_MANAGER_TOKEN"), agent.resources.get("registration") or {}, TRANSPORT)()


def service_headers() -> dict[str, str]:
    tok = os.environ.get("ATLAS_MANAGER_TOKEN", "")
    return {"Authorization": f"Bearer {tok}"} if tok and "CHANGE_ME" not in tok else {}


async def mgr_call(method: str, path: str, body: Any = None, params: dict[str, Any] | None = None, as_agent: bool = False) -> httpx.Response:
    mgr = url("ATLAS_MANAGER_URL", "http://manager:8080")
    h = service_headers()
    if as_agent and (tok := await manager_token()):
        h = {"Authorization": f"Bearer {tok}"}
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
        return await c.request(method, mgr + path, json=body, params=params, headers=otel.inject(h))


@router.post("/templates/{name}/versions/{version}/submit")
async def submit(name: str, version: str):
    """게시 신청 → Manager 승인함(X-HITL) 요청. 정적 스캔에서 차단 점수면 신청부터 거부."""
    t = get_row(name, version)
    if t.status not in ("draft", "rejected"):
        raise HTTPException(409, f"상태 {t.status} — draft 만 신청")
    if t.scan.get("score", 0) >= int(cfg()["scanBlockScore"]):
        raise HTTPException(422, f"정적 스캔 차단 ({t.scan['rules']}) — 악성 지시 의심")
    if not t.sbom:
        raise HTTPException(422, "SBOM 첨부 필수 (manifest.sbom)")
    reg = agent.resources.get("registration") or {}
    r = await mgr_call("POST", "/api/v1/approvals", {"source": "mkt", "sourceRef": f"{t.name}@{t.version}", "agentId": reg.get("agent_id", ""),
                                                    "unitId": "X-MKT", "action": "mkt.publish", "riskLevel": "medium", "project": t.project,
                                                    "payload": {"template": t.name, "version": t.version, "kind": t.kind, "visibility": t.visibility,
                                                                "digest": t.digest, "scan": t.scan}, "expiresInS": 7 * 86400},
                       params={"org": t.tenant}, as_agent=True)
    if r.status_code >= 300:
        raise HTTPException(502, f"승인 요청 실패 {r.status_code}: {r.text[:200]}")
    with Session(engine()) as s, s.begin():
        row = s.get(TemplateRow, t.id)
        row.status, row.approval_id = "pending", r.json()["id"]
    return {"status": "pending", "approval_id": r.json()["id"]}


async def reconcile() -> list[dict[str, Any]]:
    """승인 결정 반영: approved/edited → 서명·게시, rejected/expired → rejected."""
    out = []
    with Session(engine()) as s:
        pending = list(s.scalars(select(TemplateRow).where(TemplateRow.status == "pending")))
    for t in pending:
        r = await mgr_call("GET", f"/api/v1/approvals/{t.approval_id}", params={"org": t.tenant}, as_agent=True)
        if r.status_code == 403:
            r = await mgr_call("GET", f"/api/v1/approvals/{t.approval_id}", params={"org": t.tenant})
        if r.status_code != 200:
            continue
        st = r.json()["approval"]["status"]
        if st == "pending":
            continue
        with Session(engine()) as s, s.begin():
            row = s.get(TemplateRow, t.id)
            if st in ("approved", "edited"):
                sign_row(row, "org-publish")
                row.status, row.published_at = "published", datetime.now(UTC)
            else:
                row.status = "rejected"
            out.append({"template": f"{row.name}@{row.version}", "status": row.status})
        if st in ("approved", "edited"):
            await events.publish(agent, "plt.mkt.published", {"template": t.name, "version": t.version, "visibility": t.visibility}, t.tenant)
    return out


@router.post("/templates/reconcile")
async def reconcile_now():
    return {"items": await reconcile()}


class DeprecateIn(BaseModel):
    replaced_by: str | None = None
    reason: str = ""


@router.post("/templates/{name}/versions/{version}/deprecate")
async def deprecate(name: str, version: str, body: DeprecateIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    t = get_row(name, version)
    with Session(engine()) as s, s.begin():
        row = s.get(TemplateRow, t.id)
        row.status, row.replaced_by = "deprecated", body.replaced_by
    await events.publish(agent, "plt.mkt.deprecated", {"template": name, "version": version, "replaced_by": body.replaced_by, "reason": body.reason}, t.tenant)
    return {"status": "deprecated", "replaced_by": body.replaced_by}


class KeyIn(BaseModel):
    public_key: str
    owner: str


@admin_router.put("/keys/{kid}")
async def put_key(kid: str, body: KeyIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    if signing.kid_of(body.public_key) != kid:
        raise HTTPException(422, f"kid 는 공개키 지문이어야 함 ({signing.kid_of(body.public_key)})")
    with Session(engine()) as s, s.begin():
        s.merge(KeyRow(kid=kid, public_key=body.public_key, owner=body.owner, source="publisher"))
    return {"kid": kid, "owner": body.owner}


@router.get("/keys")
async def list_keys():
    with Session(engine()) as s:
        return {"items": [{"kid": k.kid, "owner": k.owner, "source": k.source, "public_key": k.public_key} for k in s.scalars(select(KeyRow))]}


# ---- 설치 (MKT-02) ----

def render(obj: Any, params: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        whole = re.fullmatch(r"\{\{params\.(\w+)\}\}", obj)
        if whole:
            return params.get(whole.group(1))
        return re.sub(r"\{\{params\.(\w+)\}\}", lambda m: str(params.get(m.group(1), "")), obj)
    if isinstance(obj, dict):
        return {k: render(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, params) for v in obj]
    return obj


class InstallIn(BaseModel):
    template: str
    version: str | None = None
    tenant: str = "default"
    project: str = "default"
    params: dict[str, Any] = {}


def with_defaults(sch: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    out = {k: v["default"] for k, v in sch.get("properties", {}).items() if "default" in v}
    return {**out, **params}


async def apply(t: TemplateRow, tenant: str, project: str, params: dict[str, Any], install_id: str) -> dict[str, Any]:
    payload = render(t.manifest["payload"], params)
    if t.kind == "agent":
        r = await mgr_call("POST", "/api/v1/agents", payload["manifest"], params={"project": project, "org": tenant})
        if r.status_code == 409 or (r.status_code == 400 and "exists" in r.text):
            raise HTTPException(409, f"같은 이름 에이전트 존재: {payload['manifest']['metadata']['name']}")
        if r.status_code >= 300:
            raise HTTPException(502, f"Manager 등록 실패 {r.status_code}: {r.text[:300]}")
        ag = r.json()
        d = payload.get("deploy") or {}
        dr = await mgr_call("POST", "/api/v1/deployments", {"agentId": ag["id"], "env": d.get("env", "dev"), "driver": d.get("driver", "compose"),
                                                            "endpoint": d.get("endpoint")}, params={"org": tenant})
        return {"agent_id": ag["id"], "agent_name": ag.get("name"), "deployment_id": dr.json().get("id") if dr.status_code < 300 else None,
                "deployment_status": dr.json().get("status") if dr.status_code < 300 else f"error {dr.status_code}"}
    if t.kind == "mcp-server":
        tok = await secrets.get("mcp-reg-admin-token", env="ATLAS_MCP_REG_ADMIN_TOKEN", dev_default="")
        async with httpx.AsyncClient(timeout=60, transport=TRANSPORT) as c:
            r = await c.post(url("ATLAS_MCP_REGISTRY_URL", "http://agent-mcp-reg:8000") + "/admin/servers", json=payload["server"],
                             headers={"authorization": f"Bearer {tok}"})
        if r.status_code >= 300:
            raise HTTPException(502, f"MCP-REG 등록 실패 {r.status_code}: {r.text[:300]}")
        st = r.json().get("_meta", {}).get("io.prodai.atlas/registry", {}).get("status")
        return {"mcp_server": f"{payload['server']['name']}@{payload['server']['version']}", "registry_status": st}
    return {"artifact": t.kind, "delivered": True, "download": f"/v1/installs/{install_id}/artifact"}


@router.post("/installs", status_code=201)
async def install(body: InstallIn):
    t = latest(body.template, body.tenant, body.project) if not body.version else get_row(body.template, body.version)
    if t is None or not visible(t, body.tenant, body.project):
        raise HTTPException(404, f"템플릿 {body.template} 없음")
    if t.status == "deprecated":
        raise HTTPException(409, f"폐기된 버전 — 대체: {t.replaced_by or '없음'}")
    if t.status != "published":
        raise HTTPException(409, f"게시되지 않은 템플릿 (상태 {t.status})")
    v = verify_row(t)
    if not v["ok"]:
        await events.publish(agent, "plt.mkt.install_blocked", {"template": t.name, "version": t.version, "reasons": v["reasons"]}, body.tenant)
        raise HTTPException(403, {"message": "공급망 검증 실패 — 설치 차단", "verification": v})
    sch = t.manifest["params"]
    params = with_defaults(sch, body.params)
    errs = sorted(jsonschema.Draft202012Validator(sch).iter_errors(params), key=lambda e: list(e.path))
    if errs:
        raise HTTPException(422, {"message": "파라미터 오류", "errors": [{"path": "/".join(map(str, e.path)), "message": e.message} for e in errs]})
    import uuid as _uuid
    iid = str(_uuid.uuid4())
    stored = dict(params)
    for k, p in sch.get("properties", {}).items():       # 비밀 파라미터 → Manager 비밀 (설치 기록에는 참조만)
        if p.get("x-atlas-secret") and params.get(k):
            sname = f"svc-mkt-{iid[:8]}-{k.replace('_', '-')}"     # svc- 접두 = 서비스 키(secrets:write)가 쓸 수 있는 범위
            r = await mgr_call("PUT", f"/api/v1/orgs/{body.tenant}/secrets/{sname}", {"value": str(params[k])})
            if r.status_code >= 300:
                raise HTTPException(502, f"비밀 저장 실패 {r.status_code}: {r.text[:200]}")
            stored[k] = {"secretRef": sname}
    target = await apply(t, body.tenant, body.project, params, iid)
    with Session(engine()) as s, s.begin():
        row = InstallRow(id=iid, template=t.name, version=t.version, tenant=body.tenant, project=body.project, params=stored, status="installed",
                         target=target, history=[{"at": datetime.now(UTC).isoformat(), "action": "install", "version": t.version}])
        s.add(row)
    await events.publish(agent, "plt.mkt.installed", {"install": iid, "template": t.name, "version": t.version, "project": body.project, "kind": t.kind}, body.tenant)
    return {"id": iid, "template": t.name, "version": t.version, "status": "installed", "params": stored, "target": target, "verification": v}


def install_view(i: InstallRow) -> dict[str, Any]:
    return {"id": i.id, "template": i.template, "version": i.version, "tenant": i.tenant, "project": i.project, "params": i.params,
            "status": i.status, "target": i.target, "history": i.history, "created_at": i.created_at.isoformat()}


def get_install(iid: str) -> InstallRow:
    with Session(engine()) as s:
        i = s.get(InstallRow, iid)
    if i is None:
        raise HTTPException(404, "설치 기록 없음")
    return i


@router.get("/installs")
async def list_installs(tenant: str = "default", project: str | None = None):
    with Session(engine()) as s:
        q = select(InstallRow).where(InstallRow.tenant == tenant)
        if project:
            q = q.where(InstallRow.project == project)
        return {"items": [install_view(i) for i in s.scalars(q.order_by(InstallRow.created_at.desc()))]}


@router.get("/installs/{iid}")
async def get_install_view(iid: str):
    return install_view(get_install(iid))


@router.get("/installs/{iid}/artifact")
async def artifact(iid: str):
    i = get_install(iid)
    t = get_row(i.template, i.version)
    return {"kind": t.kind, "payload": render(t.manifest["payload"], {k: v for k, v in i.params.items() if not isinstance(v, dict)})}


# ---- 업데이트 알림·업그레이드 (MKT-05) ----

def updates_for(i: InstallRow) -> dict[str, Any]:
    newer = sorted([t for t in rows(i.template) if t.status == "published" and visible(t, i.tenant, i.project) and semver(t.version) > semver(i.version)],
                   key=lambda t: semver(t.version))
    return {"install": i.id, "template": i.template, "current": i.version, "latest": newer[-1].version if newer else i.version,
            "available": bool(newer), "changelog": [{"version": t.version, "changes": t.manifest.get("changelog", "")} for t in newer]}


@router.get("/installs/{iid}/updates")
async def updates(iid: str):
    return updates_for(get_install(iid))


async def check_updates() -> list[dict[str, Any]]:
    out = []
    with Session(engine()) as s:
        installs = list(s.scalars(select(InstallRow).where(InstallRow.status == "installed")))
    for i in installs:
        u = updates_for(i)
        if u["available"] and i.notified_version != u["latest"]:
            await events.publish(agent, "plt.mkt.update_available", {"install": i.id, "template": i.template, "from": i.version, "to": u["latest"],
                                                                     "project": i.project, "changelog": u["changelog"]}, i.tenant)
            with Session(engine()) as s, s.begin():
                s.get(InstallRow, i.id).notified_version = u["latest"]
            out.append(u)
    return out


@router.post("/updates/check")
async def check_now():
    return {"notified": await check_updates()}


class UpgradeIn(BaseModel):
    version: str | None = None


async def regression(t: TemplateRow) -> dict[str, Any] | None:
    ev = (t.manifest.get("eval") or {}).get("suite")
    if not ev:
        return None
    async with httpx.AsyncClient(timeout=300, transport=TRANSPORT) as c:
        r = await c.post(url("ATLAS_EV_URL", "http://agent-ev:8000") + "/v1/runs", content=json.dumps(ev), headers=otel.inject({"content-type": "application/json"}))
    if r.status_code >= 300:
        return {"gate": "error", "error": f"{r.status_code}: {r.text[:200]}"}
    b = r.json()
    return {"gate": b.get("gate"), "run_id": b.get("run_id"), "pass_rate": (b.get("summary") or {}).get("pass_rate")}


@router.post("/installs/{iid}/upgrade")
async def upgrade(iid: str, body: UpgradeIn):
    i = get_install(iid)
    u = updates_for(i)
    to = body.version or u["latest"]
    if semver(to) <= semver(i.version):
        raise HTTPException(409, f"이미 최신 ({i.version})")
    t = get_row(i.template, to)
    if t.status != "published":
        raise HTTPException(409, f"{to} 상태 {t.status}")
    v = verify_row(t)
    if not v["ok"]:
        raise HTTPException(403, {"message": "공급망 검증 실패", "verification": v})
    reg = await regression(t)
    if cfg()["upgradeRequireEvalPass"] and reg is not None and reg["gate"] != "pass":
        with Session(engine()) as s, s.begin():
            row = s.get(InstallRow, iid)
            row.history = [*row.history, {"at": datetime.now(UTC).isoformat(), "action": "upgrade_blocked", "version": to, "eval": reg}]
        raise HTTPException(409, {"message": "회귀 평가 미통과 — 업그레이드 차단 (mkt.upgrade.require_eval_pass)", "eval": reg})
    params = {k: v for k, v in i.params.items() if not isinstance(v, dict)}
    target = i.target
    if t.kind == "agent" and i.target.get("agent_id"):     # 같은 에이전트에 새 리비전 (PATCH)
        manifest = render(t.manifest["payload"], with_defaults(t.manifest["params"], params))["manifest"]
        r = await mgr_call("PATCH", f"/api/v1/agents/{i.target['agent_id']}", manifest, params={"org": i.tenant})
        if r.status_code >= 300:
            raise HTTPException(502, f"Manager 갱신 실패 {r.status_code}: {r.text[:200]}")
        target = {**i.target, "rev": (r.json().get("agent") or {}).get("currentRev")}
    with Session(engine()) as s, s.begin():
        row = s.get(InstallRow, iid)
        row.history = [*row.history, {"at": datetime.now(UTC).isoformat(), "action": "upgrade", "from": row.version, "version": to, "eval": reg}]
        row.version, row.target, row.updated_at = to, target, datetime.now(UTC)
    await events.publish(agent, "plt.mkt.upgraded", {"install": iid, "template": i.template, "from": i.version, "to": to, "eval": reg}, i.tenant)
    return {"id": iid, "from": i.version, "version": to, "eval": reg, "target": target}


# ---- MCP 서버 카탈로그 (MKT-06) ----

@router.post("/mcp/sync")
async def mcp_sync(authorization: str | None = Header(None)):
    await require_admin(authorization)
    base, n, cursor = cfg()["mcpRegistryUrl"].rstrip("/"), 0, None
    async with httpx.AsyncClient(timeout=30, transport=TRANSPORT) as c:
        for _ in range(20):
            r = await c.get(f"{base}/v0/servers", params={"limit": 100, **({"cursor": cursor} if cursor else {})})
            r.raise_for_status()
            b = r.json()
            with Session(engine()) as s, s.begin():
                for e in b.get("servers", []):
                    sj = e.get("server", e)
                    row = s.get(McpCatalogRow, (sj["name"], sj["version"]))
                    if row is None:
                        s.add(McpCatalogRow(name=sj["name"], version=sj["version"], server_json=sj))
                        n += 1
                    else:
                        row.server_json, row.synced_at = sj, datetime.now(UTC)
            cursor = (b.get("metadata") or {}).get("nextCursor")
            if not cursor:
                break
    return {"added": n, "total": len((await mcp_catalog())["items"])}


@router.get("/mcp/catalog")
async def mcp_catalog(q: str | None = None):
    with Session(engine()) as s:
        items = [{"name": r.name, "version": r.version, "description": r.server_json.get("description", ""), "status": r.status,
                  "remotes": r.server_json.get("remotes", []), "review": r.review} for r in s.scalars(select(McpCatalogRow))]
    return {"items": [i for i in items if not q or q.lower() in (i["name"] + i["description"]).lower()]}


class ImportIn(BaseModel):
    name: str
    version: str
    trust_level: str = Field(pattern="^(unverified|community|verified|official)$")
    reviewer: str = Field(min_length=1)
    permissions_reviewed: bool
    scopes: list[str] = []            # 검토한 권한 범위(네트워크·데이터·쓰기 도구)
    note: str = ""


@router.post("/mcp/import")
async def mcp_import(body: ImportIn, authorization: str | None = Header(None)):
    await require_admin(authorization)
    if not body.permissions_reviewed or not body.scopes:
        raise HTTPException(422, "권한 범위 검토 필수 (permissions_reviewed=true · scopes[])")
    with Session(engine()) as s:
        row = s.get(McpCatalogRow, (body.name, body.version))
    if row is None:
        raise HTTPException(404, "카탈로그에 없음 — POST /v1/mcp/sync")
    sj = {k: row.server_json[k] for k in ("name", "version", "description", "remotes", "packages") if k in row.server_json}
    tok = await secrets.get("mcp-reg-admin-token", env="ATLAS_MCP_REG_ADMIN_TOKEN", dev_default="")
    h = {"authorization": f"Bearer {tok}"}
    reg_url = url("ATLAS_MCP_REGISTRY_URL", "http://agent-mcp-reg:8000")
    async with httpx.AsyncClient(timeout=60, transport=TRANSPORT) as c:
        r = await c.post(f"{reg_url}/admin/servers", json=sj, headers=h)
        if r.status_code >= 300:
            raise HTTPException(502, f"MCP-REG 등록 실패 {r.status_code}: {r.text[:200]}")
        st = r.json().get("_meta", {}).get("io.prodai.atlas/registry", {}).get("status")
        ap = None
        if st == "scanned":
            ap = await c.post(f"{reg_url}/admin/servers/{sj['name']}/versions/{sj['version']}/approve", json={"trust": body.trust_level}, headers=h)
    status = "imported" if ap is not None and ap.status_code < 300 else "rejected"
    review = {"reviewer": body.reviewer, "trust_level": body.trust_level, "scopes": body.scopes, "note": body.note, "registry_status": st,
              "at": datetime.now(UTC).isoformat()}
    with Session(engine()) as s, s.begin():
        x = s.get(McpCatalogRow, (body.name, body.version))
        x.status, x.review = status, review
    return {"name": body.name, "version": body.version, "status": status, "registry_status": st, "review": review}


# ---- 백그라운드: 번들 동기화 · 승인 반영 · 업데이트 알림 ----

async def worker() -> None:
    every = float(os.environ.get("ATLAS_MKT_INTERVAL_S", "30"))
    for _ in range(30):
        try:
            sync_bundled()
            break
        except Exception as exc:  # noqa: BLE001 — DB 준비 전
            log.warning("bundled sync 대기: %s", exc)
            await asyncio.sleep(2)
    while True:
        await asyncio.sleep(every)
        for job in (reconcile, check_updates):
            try:
                await job()
            except Exception as exc:  # noqa: BLE001 — 다음 주기에 다시
                log.warning("%s 실패: %s", job.__name__, exc)


async def install_skill(ctx: TaskContext) -> dict[str, Any]:
    if not ctx.data.get("template"):
        raise InputRequired("data.template(·version·project·params) 가 필요합니다")
    try:
        return await install(InstallIn(**{**ctx.data, "tenant": ctx.tenant}))
    except HTTPException as exc:
        raise ValueError(json.dumps(exc.detail, ensure_ascii=False) if not isinstance(exc.detail, str) else exc.detail) from exc


agent = AgentSpec.from_file(MANIFEST, description="X-MKT 템플릿·마켓 — 카탈로그·원클릭 설치·서명/출처 검증·사내 게시(승인)·업데이트 알림·MCP 카탈로그 동기화",
                            skills=[Skill(id="mkt.install", name="Install template", handler=install_skill,
                                          description="템플릿을 프로젝트에 설치 (서명·SBOM 검증 → 파라미터 검증 → 등록/배포)", tags=["marketplace"])],
                            routers=[router, admin_router], background=[worker])
