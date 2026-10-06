"""X-KB Knowledge & Memory — 지식베이스(수집·청킹·임베딩·하이브리드 검색·ACL) · 에이전트 메모리(4종·쓰기 검증·정리) · 삭제권 전파 · RAG 평가.

  POST·GET /v1/kbs · POST /v1/kbs/{kb}/documents · POST·GET /v1/kbs/{kb}/sources · POST /v1/kbs/{kb}/sources/{id}/sync · GET /v1/kbs/{kb}/documents
  POST /v1/kbs/{kb}/search · POST /v1/kbs/{kb}/evaluate · GET /v1/jobs/{id}
  POST·GET /v1/memory · GET /v1/memory/quarantine · POST /v1/memory/{id}/release|discard · POST /v1/memory/consolidate
  POST /v1/erasure · GET /v1/erasure/{job}
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from atlas_agent_core import (
    AgentSpec,
    InputRequired,
    Skill,
    TaskContext,
    events,
    otel,
    secrets,
)
from units.kb import pipeline
from units.kb import search as S
from units.kb.store import (
    DocRow,
    JobRow,
    KbRow,
    KbStore,
    MemoryRow,
    SourceRow,
    nid,
    now,
)

MANIFEST = Path(__file__).with_name("atlas-agent.json")
log = logging.getLogger("atlas.kb")
router = APIRouter(prefix="/v1", tags=["knowledge"])
TRANSPORT: httpx.AsyncBaseTransport | None = None   # 시험용 (GR·EV·OB·Gateway 관리)


def store() -> KbStore:
    return KbStore(agent.resources["engine"])


def http(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=TRANSPORT)


def svc(env: str, default: str) -> str:
    return (os.environ.get(env) or default).rstrip("/")


def need_kb(tenant: str, key: str) -> KbRow:
    kb = store().kb(tenant, key)
    if kb is None or kb.tenant != tenant:
        raise HTTPException(404, "kb not found")
    return kb


def principal_tags(p: dict[str, Any] | None) -> set[str] | None:
    if p is None:
        return None
    tags = {"public"}
    if p.get("user"):
        tags.add(f"user:{p['user']}")
    tags |= {g if ":" in g else f"group:{g}" for g in p.get("groups") or []}
    return tags


# ---- 지식베이스·원천 (X-KB-01·02) ----

class ChunkCfg(BaseModel):
    strategy: str = Field("paragraph", pattern="^(fixed|paragraph|heading)$")
    size: int = Field(800, ge=100, le=8000)
    overlap: int = Field(100, ge=0, le=2000)


class KbIn(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,128}$")
    description: str = ""
    chunk: ChunkCfg = ChunkCfg()
    embed_model: str = "embed-default"
    acl_enforce: bool = True
    default_acl: list[str] = ["public"]


def kb_view(k: KbRow) -> dict[str, Any]:
    docs = store().docs(k.id)
    return {"id": k.id, "tenant": k.tenant, "name": k.name, "description": k.description, "config": k.config,
            "documents": len(docs), "chunks": sum(d.chunks for d in docs), "created_at": k.created_at.isoformat()}


@router.post("/kbs", status_code=201)
async def create_kb(body: KbIn, tenant: str = "default"):
    if store().kb(tenant, body.name):
        raise HTTPException(409, "같은 이름의 지식베이스가 있음")
    k = store().add(KbRow(id=nid(), tenant=tenant, name=body.name, description=body.description, created_at=now(),
                          config={"chunk": body.chunk.model_dump(), "embed_model": body.embed_model, "acl_enforce": body.acl_enforce, "default_acl": body.default_acl}))
    return kb_view(k)


@router.get("/kbs")
async def list_kbs(tenant: str = "default"):
    return {"items": [kb_view(k) for k in store().kbs(tenant)]}


async def index_doc(kb: KbRow, uri: str, content: str, mime: str, source_id: str | None = None, acl: list[str] | None = None,
                    subjects: list[str] | None = None) -> DocRow:
    """parse → (해시 비교·중복) → chunk → embed → index. 같은 uri 가 바뀌면 다시 인덱싱."""
    p = pipeline.parse(content, mime)
    h = pipeline.sha(p["text"])
    st = store()
    doc = st.doc_by_uri(kb.id, uri)
    tags = acl or p["acl"] or kb.config.get("default_acl") or ["public"]
    subj = sorted(set((subjects or []) + p["subjects"]))
    if doc and doc.sha256 == h:
        return doc
    if doc is None:
        doc = st.add(DocRow(id=nid(), kb_id=kb.id, source_id=source_id, uri=uri, sha256=h, title=p["title"], acl_tags=tags, subject_ids=subj,
                            duplicate_of=None, status="queued", chunks=0, updated_at=now()))
    else:
        doc = st.update(DocRow, doc.id, sha256=h, title=p["title"], acl_tags=tags, subject_ids=subj, status="queued", duplicate_of=None)
    dup = st.doc_by_sha(kb.id, h, exclude=doc.id)
    if dup:                                                   # 같은 내용은 한 번만 인덱싱 (sha256 중복 제거)
        st.replace_chunks(doc, [])
        return st.update(DocRow, doc.id, status="duplicate", duplicate_of=dup.id, chunks=0, updated_at=now())
    cfg = kb.config["chunk"]
    parts = pipeline.chunk(p["text"], cfg["strategy"], cfg["size"], cfg["overlap"])
    vecs = await pipeline.embed(parts, kb.config["embed_model"])
    st.replace_chunks(doc, [{"text": t, "embedding": v} for t, v in zip(parts, vecs)])
    return st.update(DocRow, doc.id, status="indexed", chunks=len(parts), updated_at=now())


class DocIn(BaseModel):
    uri: str
    content: str = Field(min_length=1)
    mime: str = "text/markdown"
    acl_tags: list[str] | None = None
    subject_ids: list[str] | None = None
    wait: bool = True


def doc_view(d: DocRow) -> dict[str, Any]:
    return {"id": d.id, "uri": d.uri, "title": d.title, "sha256": d.sha256, "status": d.status, "chunks": d.chunks, "acl_tags": d.acl_tags,
            "subject_ids": d.subject_ids, "duplicate_of": d.duplicate_of, "updated_at": d.updated_at.isoformat()}


@router.post("/kbs/{kb}/documents", status_code=201)
async def upload(kb: str, body: DocIn, tenant: str = "default"):
    """파일 업로드 커넥터 — wait=false 면 비동기 작업 큐(index)로."""
    k = need_kb(tenant, kb)
    if not body.wait:
        j = store().add(JobRow(id=nid(), kind="index", kb_id=k.id, status="queued", payload=body.model_dump(), created_at=now()))
        return {"job_id": j.id, "status": "queued"}
    return doc_view(await index_doc(k, body.uri, body.content, body.mime, None, body.acl_tags, body.subject_ids))


@router.get("/kbs/{kb}/documents")
async def list_docs(kb: str, tenant: str = "default"):
    return {"items": [doc_view(d) for d in store().docs(need_kb(tenant, kb).id)]}


class SourceIn(BaseModel):
    type: str = Field(pattern="^(corpus|url)$")
    config: dict[str, Any] = {}
    interval_s: int = Field(0, ge=0)


def src_view(s: SourceRow) -> dict[str, Any]:
    return {"id": s.id, "type": s.type, "config": s.config, "interval_s": s.interval_s, "tracked": len(s.cursor),
            "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None}


@router.post("/kbs/{kb}/sources", status_code=201)
async def add_source(kb: str, body: SourceIn, tenant: str = "default"):
    k = need_kb(tenant, kb)
    if body.type == "url":
        bad = [u for u in body.config.get("urls", []) if not pipeline.allowed_url(u)]
        if bad:
            raise HTTPException(422, f"허용되지 않은 URL(SSRF 차단): {bad}")
    return src_view(store().add(SourceRow(id=nid(), kb_id=k.id, type=body.type, config=body.config, cursor={}, interval_s=body.interval_s,
                                          created_at=now())))


@router.get("/kbs/{kb}/sources")
async def list_sources(kb: str, tenant: str = "default"):
    return {"items": [src_view(s) for s in store().sources(need_kb(tenant, kb).id)]}


async def sync_source(src: SourceRow) -> dict[str, Any]:
    """증분 동기화: list → fetch → 해시가 바뀐 것만 인덱싱 · 원천에서 사라진 문서 삭제 · 커서 저장."""
    st = store()
    kb = st.get(KbRow, src.kb_id)
    items = await pipeline.source_list(src)
    cursor, out = dict(src.cursor), {"listed": len(items), "added": 0, "updated": 0, "unchanged": 0, "deleted": 0, "duplicates": 0, "errors": []}
    seen = set()
    for it in items:
        seen.add(it["uri"])
        try:
            content = await pipeline.source_fetch(src, it)
        except Exception as exc:  # noqa: BLE001 — 문서별 오류는 보고만
            out["errors"].append({"uri": it["uri"], "error": str(exc)[:200]})
            continue
        h = pipeline.sha(pipeline.parse(content, it["mime"])["text"])
        if cursor.get(it["uri"]) == h:
            out["unchanged"] += 1
            continue
        d = await index_doc(kb, it["uri"], content, it["mime"], src.id)
        out["duplicates" if d.status == "duplicate" else "updated" if it["uri"] in cursor else "added"] += 1
        cursor[it["uri"]] = h
    for uri in [u for u in cursor if u not in seen]:
        d = st.doc_by_uri(kb.id, uri)
        if d:
            st.delete_doc(d.id)
        cursor.pop(uri)
        out["deleted"] += 1
    st.update(SourceRow, src.id, cursor=cursor, last_sync_at=now())
    return out


@router.post("/kbs/{kb}/sources/{sid}/sync")
async def sync_now(kb: str, sid: str, tenant: str = "default"):
    k = need_kb(tenant, kb)
    src = store().get(SourceRow, sid)
    if src is None or src.kb_id != k.id:
        raise HTTPException(404, "source not found")
    return await sync_source(src)


@router.get("/jobs/{jid}")
async def get_job(jid: str):
    j = store().get(JobRow, jid)
    if j is None:
        raise HTTPException(404, "job not found")
    return {"id": j.id, "kind": j.kind, "status": j.status, "result": j.result, "created_at": j.created_at.isoformat()}


# ---- 검색 (X-KB-03·04) ----

class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    k: int = Field(5, ge=1, le=50)
    principal: dict[str, Any] | None = None     # {user, groups[]} — 위임 신원
    rerank: bool = False


async def do_search(kb: KbRow, body: SearchIn) -> dict[str, Any]:
    tags = principal_tags(body.principal)
    if kb.config.get("acl_enforce", True) and tags is None:
        raise HTTPException(403, "ACL 강제: principal(위임 신원) 없는 검색 거부 (kb.acl.enforce)")
    qv = (await pipeline.embed([body.query], kb.config["embed_model"]))[0]
    hits = S.search(store(), kb.id, body.query, qv, body.k, tags, do_rerank=body.rerank)
    return {"kb": kb.name, "query": body.query, "items": hits}


@router.post("/kbs/{kb}/search")
async def kb_search(kb: str, body: SearchIn, tenant: str = "default"):
    return await do_search(need_kb(tenant, kb), body)


# ---- 에이전트 메모리 (X-KB-05·06·07) ----

class Provenance(BaseModel):
    trace_id: str | None = None
    tool: str | None = None
    user: str | None = None
    confidence: float = Field(1.0, ge=0, le=1)


class MemoryIn(BaseModel):
    scope: str = Field(pattern="^(user|agent|session)$")
    scope_id: str = Field(min_length=1, max_length=128)
    type: str = Field(pattern="^(working|episodic|semantic|procedural)$")
    content: str = Field(min_length=1, max_length=8000)
    provenance: Provenance | None = None
    subject_id: str | None = None
    tenant: str = "default"


def mem_view(m: MemoryRow) -> dict[str, Any]:
    return {"id": m.id, "scope": m.scope, "scope_id": m.scope_id, "type": m.type, "content": m.content, "status": m.status, "reason": m.reason,
            "confidence": m.confidence, "provenance": m.provenance, "versions": len(m.history) + 1, "updated_at": m.updated_at.isoformat()}


async def guard_injection(tenant: str, content: str) -> str | None:
    """쓰기 전 인젝션 분류 (GR-IO) — 의심이면 사유, 정상이면 None. 가드 장애 시 None(fail-open, 출처·신뢰도 검사는 유지)."""
    try:
        async with http(10) as c:
            r = await c.post(svc("ATLAS_GUARD_URL", "http://agent-gr:8000") + "/v1/guard/check", json={"tenant": tenant, "boundary": "input", "contents": [content]},
                             headers=otel.inject({}))
        r.raise_for_status()
        v = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    cats = set(v.get("categories") or [])
    if v.get("decision") == "block" or cats & {"jailbreak", "prompt_injection", "injection"}:
        return f"가드 판정 {v.get('decision')} {sorted(cats)}"
    return None


FORGET = re.compile(r"^\s*(forget|잊어|삭제)\s*[:：]\s*", re.IGNORECASE)


@router.post("/memory", status_code=201)
async def write_memory(body: MemoryIn):
    """메모리 쓰기 — 출처 필수·신뢰도 0.5 미만·인젝션 의심은 격리(quarantine, 검색 제외) → 그 외 ADD·UPDATE·DELETE (Mem0 식 통합)."""
    st = store()
    prov = body.provenance.model_dump() if body.provenance else {}
    conf = float(prov.get("confidence", 0.0)) if prov else 0.0
    reason = None
    if not prov or not (prov.get("trace_id") or prov.get("tool") or prov.get("user")):
        reason = "출처(provenance) 없음"
    elif conf < 0.5:
        reason = f"신뢰도 {conf} < 0.5"
    else:
        reason = await guard_injection(body.tenant, body.content)
    forget = FORGET.match(body.content)
    content = body.content[forget.end():] if forget else body.content
    vec = (await pipeline.embed([content]))[0]
    if reason:
        m = st.add(MemoryRow(id=nid(), tenant=body.tenant, scope=body.scope, scope_id=body.scope_id, type=body.type, content=body.content, embedding=vec,
                             provenance=prov, confidence=conf, status="quarantined", reason=reason, subject_id=body.subject_id, history=[], created_at=now(),
                             updated_at=now()))
        return {"action": "QUARANTINE", **mem_view(m)}
    same = [m for m in st.memories(body.tenant, scope=body.scope, scope_id=body.scope_id, type=body.type, status="active")]
    best = max(same, key=lambda m: S.cosine(vec, m.embedding), default=None)
    sim = S.cosine(vec, best.embedding) if best else 0.0
    if forget:
        if not best or sim < 0.6:
            raise HTTPException(404, "지울 메모리를 찾지 못함")
        m = st.update(MemoryRow, best.id, status="deleted", history=[*best.history, {"content": best.content, "at": now().isoformat(), "op": "DELETE"}], updated_at=now())
        return {"action": "DELETE", **mem_view(m)}
    if best and sim >= 0.85:
        m = st.update(MemoryRow, best.id, content=content, embedding=vec, provenance=prov, confidence=conf,
                      history=[*best.history, {"content": best.content, "at": now().isoformat(), "op": "UPDATE", "similarity": round(sim, 4)}], updated_at=now())
        return {"action": "UPDATE", **mem_view(m)}
    m = st.add(MemoryRow(id=nid(), tenant=body.tenant, scope=body.scope, scope_id=body.scope_id, type=body.type, content=content, embedding=vec, provenance=prov,
                         confidence=conf, status="active", reason=None, subject_id=body.subject_id, history=[], created_at=now(), updated_at=now()))
    return {"action": "ADD", **mem_view(m)}


@router.get("/memory")
async def read_memory(scope: str, scope_id: str, type: str | None = None, q: str | None = None, k: int = 10, tenant: str = "default"):
    """다음 세션 불러오기 — active 만(격리·삭제 제외). q 가 있으면 의미 유사도 순."""
    rows = store().memories(tenant, scope=scope, scope_id=scope_id, type=type, status="active")
    if q:
        qv = (await pipeline.embed([q]))[0]
        rows = sorted(rows, key=lambda m: -S.cosine(qv, m.embedding))
    else:
        rows = sorted(rows, key=lambda m: m.updated_at, reverse=True)
    return {"items": [mem_view(m) for m in rows[: min(k, 100)]]}


@router.get("/memory/quarantine")
async def quarantine(tenant: str = "default"):
    return {"items": [mem_view(m) for m in store().memories(tenant, status="quarantined")]}


@router.post("/memory/{mid}/{action}")
async def review_memory(mid: str, action: str):
    if action not in ("release", "discard"):
        raise HTTPException(404, "action = release | discard")
    m = store().get(MemoryRow, mid)
    if m is None or m.status != "quarantined":
        raise HTTPException(409, "격리 상태가 아님")
    m = store().update(MemoryRow, mid, status="active" if action == "release" else "deleted", reason=f"{m.reason} → 검토 {action}", updated_at=now())
    return mem_view(m)


def merge_text(a: str, b: str) -> str:
    seen, out = set(), []
    for s in re.split(r"(?<=[.!?。])\s+|\n+", f"{a}\n{b}"):
        k = s.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return " ".join(out)


async def consolidate(tenant: str = "default", threshold: float = 0.8) -> dict[str, Any]:
    """비동기 정리 (FR-X-KB-07): 같은 scope·type 의 유사 메모리를 최신 항목으로 병합 → 나머지는 merged. 변경 diff 를 결과(감사)로 남긴다."""
    st = store()
    groups: dict[tuple, list[MemoryRow]] = {}
    for m in st.memories(tenant, status="active"):
        groups.setdefault((m.scope, m.scope_id, m.type), []).append(m)
    diffs = []
    for ms in groups.values():
        ms = sorted(ms, key=lambda m: m.updated_at, reverse=True)
        used: set[str] = set()
        for keep in ms:
            if keep.id in used:
                continue
            for other in ms:
                if other.id == keep.id or other.id in used or S.cosine(keep.embedding, other.embedding) < threshold:
                    continue
                merged = merge_text(keep.content, other.content)
                vec = (await pipeline.embed([merged]))[0]
                keep = st.update(MemoryRow, keep.id, content=merged, embedding=vec,
                                 history=[*keep.history, {"content": keep.content, "at": now().isoformat(), "op": "MERGE", "from": other.id}], updated_at=now())
                st.update(MemoryRow, other.id, status="merged", reason=f"병합 → {keep.id}", updated_at=now())
                used |= {other.id}
                diffs.append({"kept": keep.id, "merged": other.id, "content": merged})
    job = st.add(JobRow(id=nid(), kind="consolidate", kb_id=None, status="done", payload={"tenant": tenant, "threshold": threshold},
                        result={"merged": len(diffs), "diffs": diffs}, created_at=now(), finished_at=now()))
    if diffs:
        await events.publish(agent, "plt.kb.memory.consolidated", {"job": job.id, "merged": len(diffs)}, tenant)
    return {"job_id": job.id, "merged": len(diffs), "diffs": diffs}


@router.post("/memory/consolidate")
async def consolidate_now(tenant: str = "default", threshold: float = 0.8):
    return await consolidate(tenant, threshold)


# ---- 삭제권 전파 (X-KB-08) ----

class ErasureIn(BaseModel):
    subject_id: str = Field(min_length=3, max_length=256)
    tenant: str = "default"


@router.post("/erasure", status_code=201)
async def erasure(body: ErasureIn):
    """주체 데이터 삭제를 KB(문서·청크·벡터)·메모리·Gateway exact 캐시·trace 원문까지 순서대로 — 완료 증명서(작업 ID·건수)를 남기고 감사 이벤트."""
    st = store()
    subj = body.subject_id
    counts: dict[str, Any] = {}
    docs = 0
    for kb in st.kbs(body.tenant):
        for d in st.docs(kb.id):
            if subj in (d.subject_ids or []):
                st.delete_doc(d.id)
                docs += 1
    counts["kb_documents"] = docs
    mems = 0
    for m in st.memories(body.tenant):
        if m.status != "deleted" and (m.subject_id == subj or subj in m.content):
            st.update(MemoryRow, m.id, status="deleted", content="[erased]", embedding=[], history=[], reason="삭제권 행사", updated_at=now())
            mems += 1
    counts["memories"] = mems
    errors = {}
    try:
        tok = await secrets.get("gateway-admin-token", env="ATLAS_GATEWAY_ADMIN_TOKEN", dev_default="local-admin-token")
        async with http() as c:
            r = await c.delete(svc("ATLAS_GATEWAY_URL", "http://gateway:8080") + "/admin/econ/cache/exact", headers={"Authorization": f"Bearer {tok}"})
            r.raise_for_status()
            counts["exact_cache"] = r.json().get("deleted", 0)
    except (httpx.HTTPError, secrets.SecretUnavailable) as exc:
        errors["exact_cache"] = str(exc)[:200]
    try:
        async with http(180) as c:
            r = await c.post(svc("ATLAS_OB_URL", "http://agent-ob:8000") + "/v1/erasure", json={"subject": subj}, headers=otel.inject({}))
            r.raise_for_status()
            counts["traces"] = r.json()
    except httpx.HTTPError as exc:
        errors["traces"] = str(exc)[:200]
    cert = {"subject_sha256": hashlib.sha256(subj.encode()).hexdigest(), "counts": counts, "errors": errors, "completed_at": datetime.now(UTC).isoformat(),
            "complete": not errors}
    job = st.add(JobRow(id=nid(), kind="erasure", kb_id=None, status="done" if not errors else "error", payload={"tenant": body.tenant}, result=cert,
                        created_at=now(), finished_at=now()))
    await events.publish(agent, "plt.kb.erasure.completed", {"job": job.id, **cert}, body.tenant)
    return {"job_id": job.id, **cert}


@router.get("/erasure/{jid}")
async def erasure_cert(jid: str):
    j = store().get(JobRow, jid)
    if j is None or j.kind != "erasure":
        raise HTTPException(404, "erasure job not found")
    return {"job_id": j.id, **(j.result or {})}


# ---- RAG 평가 연동 (X-KB-09) ----

class EvalQ(BaseModel):
    q: str
    relevant: list[str] = []          # 정답 문서 uri


class EvalIn(BaseModel):
    questions: list[EvalQ] = Field(min_length=1)
    k: int = 3
    principal: dict[str, Any] | None = {"groups": []}
    drop_threshold: float = 0.1


@router.post("/kbs/{kb}/evaluate")
async def evaluate(kb: str, body: EvalIn, tenant: str = "default"):
    """KB 별 평가셋 → EV-GOLD 등록 · 질문마다 검색 → 답변(Gateway) → EV RAG judge(faithfulness·context P/R) → 직전 대비 하락 시 kb.quality.drop."""
    k = need_kb(tenant, kb)
    ev = svc("ATLAS_EV_URL", "http://agent-ev:8000")
    gw = svc("ATLAS_GATEWAY_URL", "http://gateway:8080")
    judge = f"kb-rag-{k.name}"
    ds = f"kb-eval-{k.name}"
    rows = []
    async with http(120) as c:
        if (await c.get(f"{ev}/v1/judges/{judge}")).status_code == 404:
            await c.post(f"{ev}/v1/judges", json={"name": judge, "mode": "rag", "rubric_md": "주장이 검색 문맥으로 뒷받침되는지", "threshold": 0.8})
        if (await c.get(f"{ev}/v1/datasets/{ds}/items")).status_code == 404:
            await c.post(f"{ev}/v1/datasets", json={"name": ds, "description": f"KB {k.name} RAG 평가셋 (X-KB-09)"})
            for q in body.questions:
                await c.post(f"{ev}/v1/datasets/{ds}/items", json={"input": {"q": q.q}, "expected_output": {"relevant": q.relevant}, "tags": ["kb-rag"]})
        for q in body.questions:
            hits = (await do_search(k, SearchIn(query=q.q, k=body.k, principal=body.principal)))["items"]
            ctx = [h["text"] for h in hits]
            ans = await c.post(f"{gw}/v1/chat/completions", json={"model": "chat-default", "temperature": 0, "messages": [
                {"role": "system", "content": "문맥만 근거로 답한다."}, {"role": "user", "content": "\n".join(ctx) + f"\n질문: {q.q}"}]},
                headers={"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}", "x-atlas-tags": "agent_id:kb,unit_id:X-KB"})
            output = ans.json()["choices"][0]["message"]["content"] if ans.status_code == 200 else ""
            r = (await c.post(f"{ev}/v1/judges/{judge}/evaluate", json={"output": output, "contexts": ctx, "retrieved_ids": [h["source_uri"] for h in hits],
                                                                         "relevant_ids": q.relevant})).json()
            rows.append({"q": q.q, "faithfulness": r.get("faithfulness"), "context_precision": r.get("context_precision"), "context_recall": r.get("context_recall")})
    agg = {m: round(sum(x[m] or 0 for x in rows) / len(rows), 4) for m in ("faithfulness", "context_precision", "context_recall")}
    prev = next((j for j in sorted((j for j in store_jobs("evaluate") if j.kb_id == k.id), key=lambda j: j.created_at, reverse=True)), None)
    drop = {m: round(prev.result["scores"][m] - agg[m], 4) for m in agg if prev and prev.result["scores"].get(m) is not None
            and prev.result["scores"][m] - agg[m] > body.drop_threshold} if prev else {}
    job = store().add(JobRow(id=nid(), kind="evaluate", kb_id=k.id, status="done", payload={"n": len(rows)}, result={"scores": agg, "rows": rows, "drop": drop},
                             created_at=now(), finished_at=now()))
    if drop:
        await events.publish(agent, "plt.kb.quality.drop", {"kb": k.name, "drop": drop, "scores": agg, "job": job.id}, tenant)
    return {"kb": k.name, "dataset": ds, "judge": judge, "scores": agg, "drop": drop, "rows": rows, "job_id": job.id}


def store_jobs(kind: str) -> list[JobRow]:
    from sqlalchemy import select
    with store().s() as s:
        return list(s.scalars(select(JobRow).where(JobRow.kind == kind)))


# ---- 작업 큐 · 주기 동기화 · 야간 정리 ----

async def worker() -> None:
    last_consolidate = datetime.now(UTC)
    while True:
        await asyncio.sleep(1.0)
        st = store()
        for j in st.jobs_queued():
            st.update(JobRow, j.id, status="running")
            try:
                kb = st.get(KbRow, j.kb_id)
                d = await index_doc(kb, j.payload["uri"], j.payload["content"], j.payload.get("mime", "text/markdown"), None,
                                    j.payload.get("acl_tags"), j.payload.get("subject_ids"))
                st.update(JobRow, j.id, status="done", result=doc_view(d), finished_at=now())
            except Exception as exc:  # noqa: BLE001
                st.update(JobRow, j.id, status="error", result={"error": str(exc)[:300]}, finished_at=now())
        for src in st.sources():
            if src.interval_s and (src.last_sync_at is None or now() - src.last_sync_at.replace(tzinfo=src.last_sync_at.tzinfo or UTC) > timedelta(seconds=src.interval_s)):
                try:
                    await sync_source(src)
                except Exception as exc:  # noqa: BLE001
                    log.warning("sync %s: %s", src.id, exc)
        if datetime.now(UTC) - last_consolidate > timedelta(hours=24):   # memory.consolidate — 하루 1회
            last_consolidate = datetime.now(UTC)
            for t in {m.tenant for m in st.memories("default")} or {"default"}:
                await consolidate(t)


# ---- A2A skill ----

async def search_skill(ctx: TaskContext) -> dict[str, Any]:
    if not ctx.data.get("kb") or not ctx.data.get("query"):
        raise InputRequired("data.kb · data.query · data.principal{user,groups} 가 필요합니다")
    kb = need_kb(ctx.tenant, ctx.data["kb"])
    return await do_search(kb, SearchIn(query=ctx.data["query"], k=int(ctx.data.get("k", 5)), principal=ctx.data.get("principal"),
                                        rerank=bool(ctx.data.get("rerank"))))


async def memory_skill(ctx: TaskContext) -> dict[str, Any]:
    d = dict(ctx.data)
    d.setdefault("tenant", ctx.tenant)
    return await write_memory(MemoryIn(**d))


agent = AgentSpec.from_file(MANIFEST, description="X-KB 지식·메모리 — 수집 커넥터·청킹/임베딩·하이브리드 검색(ACL)·에이전트 메모리(쓰기 검증·정리)·삭제권 전파·RAG 평가",
                            skills=[Skill(id="kb.search", name="Knowledge search", handler=search_skill,
                                          description="지식베이스 하이브리드 검색(벡터+키워드, RRF) — 출처·ACL 태그와 함께 상위 k 청크", tags=["rag", "search"]),
                                    Skill(id="kb.memory.write", name="Write memory", handler=memory_skill,
                                          description="에이전트 메모리 쓰기 (출처·신뢰도·인젝션 검증 후 ADD/UPDATE/DELETE)", tags=["memory"])],
                            routers=[router], background=[worker])
