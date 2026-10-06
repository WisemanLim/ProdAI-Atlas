"""MA-RES Subagent A · Research — 내장 문서 코퍼스를 읽기 전용으로 조사하고 출처 필수 요약을 돌려준다 (FR-MA-RES-01·02·03)."""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext
from units.ma_common import check_spec, estimate_tokens, llm

MANIFEST = Path(__file__).with_name("atlas-agent.json")
CORPUS = Path(__file__).with_name("corpus")
_TOK = re.compile(r"[0-9a-zA-Z가-힣]+")


def tokens(s: str) -> list[str]:
    out = []
    for w in _TOK.findall(s.lower()):
        out.append(w)
        if re.search(r"[가-힣]", w) and len(w) >= 3:   # 한국어는 조사가 붙으므로 앞 2글자도 색인
            out.append(w[:2])
    return out


def load_corpus() -> list[dict[str, Any]]:
    docs = []
    for f in sorted(CORPUS.glob("*.md")):
        lines = f.read_text(encoding="utf-8").splitlines()
        title, body = lines[0].lstrip("# ").strip(), "\n".join(lines[1:])
        for i, para in enumerate(p.strip() for p in body.split("\n\n") if p.strip()):
            docs.append({"id": f"{f.stem}#p{i + 1}", "title": title, "url": f"atlas://corpus/{f.name}#p{i + 1}", "text": para, "terms": tokens(para + " " + title)})
    return docs


DOCS = load_corpus()


def search(query: str, k: int) -> list[tuple[float, dict[str, Any]]]:
    q = tokens(query)
    n = len(DOCS)
    avg = sum(len(d["terms"]) for d in DOCS) / max(n, 1)
    df = {t: sum(1 for d in DOCS if t in d["terms"]) for t in set(q)}
    scored = []
    for d in DOCS:
        s = 0.0
        for t in q:
            f = d["terms"].count(t)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(d["terms"]) / avg))
        if s > 0:
            scored.append((s, d))
    return sorted(scored, key=lambda x: -x[0])[:k]


async def research(ctx: TaskContext) -> dict[str, Any]:
    spec = check_spec(ctx.data)
    query = (spec.get("objective") or ctx.data.get("query") or ctx.text).strip()
    if not query:
        raise InputRequired("조사할 질문(query 또는 spec.objective)이 필요합니다")
    k = int(agent.manifest["spec"]["config"]["topK"])
    hits = search(query, k)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    sources = [{"id": d["id"], "url": d["url"], "title": d["title"], "retrieved_at": now} for _, d in hits]
    top = hits[0][0] if hits else 1.0
    findings = [{"claim": re.split(r"(?<=[.다])\s", d["text"], maxsplit=1)[0], "source_ids": [d["id"]], "confidence": round(min(1.0, s / top), 2)}
                for s, d in hits]
    findings = [f for f in findings if f["source_ids"]]                       # 출처 없는 주장은 버린다 (FR-MA-RES-02)
    context = "\n".join(f"[{d['id']}] {d['text']}" for _, d in hits)
    content, used = await llm([{"role": "system", "content": "출처 id 를 [id] 로 남기며 2000 토큰 이내로 요약하라. 읽기 전용."},
                               {"role": "user", "content": f"질문: {query}\n\n자료:\n{context}"}], f"agent_id:ma-res,unit_id:MA-RES,tenant:{ctx.tenant}")
    summary = content if content and not content.startswith("[mock") else " ".join(f"{f['claim']} [{f['source_ids'][0]}]" for f in findings[:3])
    return {"kind": "research", "summary": summary[:8000], "findings": findings, "sources": sources,
            "open_questions": [] if hits else [f"코퍼스에 '{query}' 관련 자료 없음"],
            "read_only": True, "usage": {"tokens": used or estimate_tokens(query + context + summary)}}


agent = AgentSpec.from_file(MANIFEST, description="MA-RES 조사 서브에이전트 — 내장 문서 코퍼스 BM25 검색, 출처 필수 요약(ResearchResult), 읽기 전용",
                            skills=[Skill(id="ma.research", name="Research", handler=research,
                                          description="질문 → ResearchResult{summary, findings[{claim, source_ids, confidence}], sources[], open_questions} research 조사 문서 출처",
                                          tags=["research", "조사"], examples=["다중 에이전트의 조정 비용이 왜 병목인가"])])
