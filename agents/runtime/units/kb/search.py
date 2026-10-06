"""하이브리드 검색·재순위 (FR-X-KB-03·04): 벡터(코사인) + 키워드(BM25/FTS) → RRF(k=60) → 선택 재순위. ACL 필터는 검색 전에 건다.

PostgreSQL: pgvector HNSW(`<=>`) + tsvector GIN. SQLite: 청크를 읽어 파이썬 코사인·BM25 (단일 노드 개발용).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from sqlalchemy import text

_TOK = re.compile(r"[\w]+", re.UNICODE)
RRF_K = 60


def tokens(s: str) -> list[str]:
    out = []
    for w in _TOK.findall(s.lower()):
        out.append(w)
        if len(w) >= 2 and "가" <= w[0] <= "힣":          # 한글: 음절 2-gram 도 (어미 변화)
            out += [w[i:i + 2] for i in range(len(w) - 1)]
    return out


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def visible(acl: list[str], principal: set[str] | None) -> bool:
    return principal is None or "public" in acl or bool(set(acl) & principal)


def bm25(query: str, docs: list[tuple[str, str]], k1: float = 1.2, b: float = 0.75) -> list[tuple[str, float]]:
    q = tokens(query)
    toks = {i: tokens(t) for i, t in docs}
    n = len(docs) or 1
    avg = sum(len(v) for v in toks.values()) / n or 1
    df = Counter(t for v in toks.values() for t in set(v))
    out = []
    for i, v in toks.items():
        tf = Counter(v)
        s = sum(math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(v) / avg)) for t in q if tf[t])
        if s > 0:
            out.append((i, s))
    return sorted(out, key=lambda x: -x[1])


def rrf(*rankings: list[str]) -> dict[str, float]:
    score: dict[str, float] = {}
    for r in rankings:
        for rank, cid in enumerate(r):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return score


def rerank(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """경량 재순위 — 질의 어휘 커버리지(cross-encoder 대체, 선택)."""
    q = set(tokens(query))
    for r in rows:
        r["rerank"] = round(len(q & set(tokens(r["text"]))) / max(1, len(q)), 4)
    return sorted(rows, key=lambda r: (-r["rerank"], -r["score"]))


def search(store, kb_id: str, query: str, qvec: list[float], k: int, principal: set[str] | None, n: int = 50, do_rerank: bool = False) -> list[dict[str, Any]]:
    if store.pg:
        acl = sorted(principal) if principal is not None else None
        f = "" if acl is None else " AND (acl_tags::jsonb ?| CAST(:acl AS text[]) OR acl_tags::jsonb ? 'public')"
        p: dict[str, Any] = {"kb": kb_id, "q": str(qvec), "n": n, **({"acl": acl} if acl is not None else {})}
        with store.engine.connect() as c:
            vec = [r[0] for r in c.execute(text(f"SELECT id FROM kb_chunks WHERE kb_id = :kb{f} ORDER BY embedding_v <=> CAST(:q AS vector) LIMIT :n"), p)]
            terms = " | ".join(sorted({t for t in _TOK.findall(query.lower()) if len(t) > 1})) or "''"
            kw = [r[0] for r in c.execute(text(f"SELECT id FROM kb_chunks WHERE kb_id = :kb{f} AND tsv @@ to_tsquery('simple', :t) "
                                                f"ORDER BY ts_rank(tsv, to_tsquery('simple', :t)) DESC LIMIT :n"), {**p, "t": terms})]
        rows = {c.id: c for c in store.chunks(kb_id) if c.id in set(vec) | set(kw)}
    else:
        allc = [c for c in store.chunks(kb_id) if visible(c.acl_tags, principal)]
        rows = {c.id: c for c in allc}
        vec = [c.id for c in sorted(allc, key=lambda c: -cosine(qvec, c.embedding))[:n]]
        kw = [i for i, _ in bm25(query, [(c.id, c.text) for c in allc])[:n]]
    fused = rrf(vec, kw)
    out = [{"chunk_id": cid, "doc_id": rows[cid].doc_id, "score": round(s, 6), "source_uri": rows[cid].source_uri, "acl_tags": rows[cid].acl_tags,
            "text": rows[cid].text, "vector_rank": vec.index(cid) + 1 if cid in vec else None, "keyword_rank": kw.index(cid) + 1 if cid in kw else None}
           for cid, s in sorted(fused.items(), key=lambda x: -x[1]) if cid in rows]
    return (rerank(query, out[: max(k * 3, k)]) if do_rerank else out)[:k]
