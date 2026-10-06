"""MCP-SEL Tool Relevance Selector — 'Tool relevance beats tool count.' (FR-MCP-SEL-01·02·03)

BM25 인덱스로 과업과 관련된 상위 k 개 도구만 컨텍스트에 싣고, 모델이 부족하면 search_tools / load_tools 로 찾아 불러온다.
임베딩 하이브리드(pgvector)·사용 통계 가중은 후속.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

from units.al_act.tools import ToolSpec

_TOKEN = re.compile(r"[a-z0-9]+|[가-힣]+")


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _TOKEN.findall(text.lower().replace("_", " ")):
        toks.append(t)
        if re.fullmatch(r"[가-힣]+", t) and len(t) > 2:          # 한국어 조사 붙은 어절 대응: 2-gram 추가
            toks += [t[i:i + 2] for i in range(len(t) - 1)]
    return toks


def doc_text(spec: ToolSpec) -> str:
    params = " ".join(spec.input_schema.get("properties", {}))
    return f"{spec.name} {spec.annotations.get('title', '')} {spec.description} {params}"


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {k: Counter(tokenize(v)) for k, v in docs.items()}
        self.len = {k: sum(c.values()) for k, c in self.tf.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 0
        df = Counter(t for c in self.tf.values() for t in c)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> dict[str, float]:
        q = tokenize(query)
        out = {}
        for k, tf in self.tf.items():
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[k] / (self.avg or 1)))
            out[k] = s
        return out


def tool_tokens(spec: ToolSpec) -> int:
    return (len(json.dumps(spec.openai(), ensure_ascii=False)) + 3) // 4


def select(task: str, tools: list[ToolSpec], k: int = 5, budget_tokens: int = 8000, pinned: list[str] | None = None) -> list[str]:
    """상위 k 개(점수 > 0) + pinned, 도구 정의 토큰 합이 예산을 넘지 않게 (FR-MCP-SEL-02)."""
    by = {t.name: t for t in tools}
    picked, used = [], 0
    for name in pinned or []:
        if name in by:
            picked.append(name)
            used += tool_tokens(by[name])
    ranked = sorted(BM25({t.name: doc_text(t) for t in tools}).scores(task).items(), key=lambda kv: (-kv[1], kv[0]))
    for name, score in ranked:
        if len(picked) >= k + len(pinned or []) or score <= 0:
            break
        cost = tool_tokens(by[name])
        if name not in picked and used + cost <= budget_tokens:
            picked.append(name)
            used += cost
    return picked


def meta_tools(catalog: list[ToolSpec]) -> list[ToolSpec]:
    """온디맨드 도구 검색 (FR-MCP-SEL-03). load_tools 는 ctx['loaded_tools'] 에 담고, 루프가 다음 턴부터 노출한다."""
    by = {t.name: t for t in catalog}

    async def search_tools(args: dict[str, Any], _ctx: dict[str, Any]) -> list[dict[str, str]]:
        names = select(args["query"], catalog, k=int(args.get("limit", 5)))
        return [{"name": n, "description": by[n].description[:200]} for n in names]

    async def load_tools(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        ok = [n for n in args["names"] if n in by]
        ctx.setdefault("loaded_tools", []).extend(ok)
        return {"loaded": ok, "unknown": [n for n in args["names"] if n not in by]}

    return [ToolSpec("search_tools", "관련 도구를 키워드로 검색한다", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]}, search_tools),
            ToolSpec("load_tools", "검색한 도구를 다음 턴부터 사용 가능하게 불러온다", {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}}, "required": ["names"]}, load_tools)]
