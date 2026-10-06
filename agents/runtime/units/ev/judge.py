"""LLM-as-Judge (FR-EV-JUDGE-01·02·03·04·08) — 루브릭 프롬프트 → Gateway alias(mock 은 결정적) → JSON 판정.

모드: binary(합/불 + critique) · pairwise(A/B 를 순서 바꿔 2회, 불일치는 tie) · rag(주장 분해 → 문맥 지지 = faithfulness).
배심원(jury): 서로 다른 alias 여러 개 → 다수결(binary) · 최다 승자(pairwise).
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

import httpx

from atlas_agent_core import otel

SYSTEM = ("You are a strict evaluation judge. Read the rubric and grade only against it. "
          "Reply with one JSON object and nothing else. binary: {\"verdict\":\"pass|fail\",\"score\":0..1,\"critique\":\"...\"} · "
          "pairwise: {\"winner\":\"A|B|tie\",\"critique\":\"...\"} · claim: {\"supported\":true|false,\"critique\":\"...\"}")


def as_text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)


def prompt(mode: str, rubric: str, instructions: str = "", **parts: Any) -> str:
    out = [f"## MODE\n{mode}", f"## RUBRIC\n{rubric.strip()}"]
    if instructions:
        out.append(f"## INSTRUCTIONS\n{instructions.strip()}")
    for title, key in (("INPUT", "input"), ("EXPECTED", "expected"), ("OUTPUT", "output"), ("OUTPUT A", "a"), ("OUTPUT B", "b"),
                       ("CLAIM", "claim"), ("CONTEXT", "context")):
        if parts.get(key) is not None:
            out.append(f"## {title}\n{as_text(parts[key])}")
    return "\n".join(out) + "\n"


def parse(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError(f"judge 응답에 JSON 없음: {text[:120]!r}")
    return json.loads(m.group(0))


async def ask(client: httpx.AsyncClient, model: str, body: str, tag: str) -> tuple[dict[str, Any], float]:
    gw = os.environ.get("ATLAS_GATEWAY_URL", "http://gateway:8080").rstrip("/")
    headers = otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}",
                           "x-atlas-tags": f"agent_id:ev-judge,unit_id:EV-JUDGE,{tag}", "x-atlas-route-pin": "true"})
    res = await client.post(f"{gw}/v1/chat/completions", headers=headers, json={
        "model": model, "temperature": 0, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": body}]})
    res.raise_for_status()
    return parse(res.json()["choices"][0]["message"].get("content") or ""), float(res.headers.get("x-atlas-cost-usd", "0") or 0)


def models_of(j: Any) -> list[str]:
    return list(j.jury) if j.jury else [j.model]


async def binary(client, j, output: Any, input_: Any = None, expected: Any = None, tag: str = "eval_run:adhoc") -> dict[str, Any]:
    body = prompt("binary", j.rubric_md, j.instructions, input=input_, expected=expected, output=output)
    votes, cost = [], 0.0
    for m in models_of(j):
        r, c = await ask(client, m, body, tag)
        cost += c
        v = str(r.get("verdict", "")).lower()
        votes.append({"model": m, "verdict": "pass" if v == "pass" else "fail", "score": float(r.get("score", 1.0 if v == "pass" else 0.0)),
                      "critique": str(r.get("critique", ""))[:1000]})
    passes = sum(v["verdict"] == "pass" for v in votes)
    passed = passes * 2 > len(votes)
    out = {"mode": "binary", "passed": passed, "verdict": "pass" if passed else "fail",
           "score": round(sum(v["score"] for v in votes) / len(votes), 4), "critique": votes[0]["critique"] if len(votes) == 1 else
           " | ".join(f"{v['model']}: {v['verdict']}" for v in votes), "cost_usd": cost}
    if len(votes) > 1:
        out["votes"], out["agreement"] = votes, round(max(passes, len(votes) - passes) / len(votes), 4)
    return out


async def pairwise(client, j, a: Any, b: Any, input_: Any = None, tag: str = "eval_run:adhoc") -> dict[str, Any]:
    """위치 편향 완화 (FR-EV-JUDGE-03): (A,B)·(B,A) 두 번 — 일치하면 채택, 어긋나면 tie + position_inconsistent."""
    winners, cost, details = [], 0.0, []
    for m in models_of(j):
        r1, c1 = await ask(client, m, prompt("pairwise", j.rubric_md, j.instructions, input=input_, a=a, b=b), tag)
        r2, c2 = await ask(client, m, prompt("pairwise", j.rubric_md, j.instructions, input=input_, a=b, b=a), tag)
        cost += c1 + c2
        w1 = str(r1.get("winner", "tie")).upper()
        w2 = {"A": "B", "B": "A"}.get(str(r2.get("winner", "tie")).upper(), "TIE")   # 뒤집은 순서를 원래 기준으로
        w1 = w1 if w1 in ("A", "B") else "TIE"
        consistent = w1 == w2
        winners.append(w1 if consistent else "TIE")
        details.append({"model": m, "first": w1, "swapped": w2, "consistent": consistent, "critique": str(r1.get("critique", ""))[:500]})
    top, n = Counter(winners).most_common(1)[0]
    winner = top if n * 2 > len(winners) else "TIE"
    return {"mode": "pairwise", "winner": winner.lower() if winner == "TIE" else winner, "position_consistent": all(d["consistent"] for d in details),
            "details": details, "cost_usd": cost}


_SENT = re.compile(r"(?<=[.!?。])\s+|\n+")


async def rag(client, j, output: Any, contexts: list[str], expected_contexts: list[str] | None = None, retrieved_ids: list[str] | None = None,
              relevant_ids: list[str] | None = None, tag: str = "eval_run:adhoc") -> dict[str, Any]:
    """RAG judge (FR-EV-JUDGE-08): faithfulness = 문맥이 지지하는 주장 비율, context precision/recall = 검색 문맥 vs 정답 문맥."""
    claims = [c.strip(" .") for c in _SENT.split(as_text(output)) if len(c.strip(" .")) >= 4]
    ctx = "\n".join(contexts)
    judged, cost = [], 0.0
    for c in claims:
        r, k = await ask(client, j.model, prompt("claim", j.rubric_md or "주장이 문맥으로 뒷받침되는지 판정", j.instructions, claim=c, context=ctx), tag)
        cost += k
        judged.append({"claim": c, "supported": bool(r.get("supported")), "critique": str(r.get("critique", ""))[:300]})
    faith = round(sum(x["supported"] for x in judged) / len(judged), 4) if judged else 0.0
    out: dict[str, Any] = {"mode": "rag", "faithfulness": faith, "claims": judged, "passed": faith >= j.threshold, "score": faith, "cost_usd": cost,
                           "critique": f"주장 {len(judged)}개 중 {sum(x['supported'] for x in judged)}개가 문맥에 근거"}
    got, rel = retrieved_ids or contexts, relevant_ids or expected_contexts
    if rel:
        hit = len(set(got) & set(rel))
        out["context_precision"] = round(hit / len(got), 4) if got else 0.0
        out["context_recall"] = round(hit / len(rel), 4)
    return out


async def evaluate(client, j, data: dict[str, Any], tag: str = "eval_run:adhoc") -> dict[str, Any]:
    if j.mode == "pairwise":
        return await pairwise(client, j, data.get("output_a", data.get("a")), data.get("output_b", data.get("b")), data.get("input"), tag)
    if j.mode == "rag":
        return await rag(client, j, data.get("output"), list(data.get("contexts") or []), data.get("expected_contexts"),
                         data.get("retrieved_ids"), data.get("relevant_ids"), tag)
    return await binary(client, j, data.get("output"), data.get("input"), data.get("expected"), tag)
