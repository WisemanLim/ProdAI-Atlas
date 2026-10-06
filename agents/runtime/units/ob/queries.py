"""OB 조회 — trace 검색·트리·세션 (FR-OB-TRACE-03·07), 대시보드·비용 귀속 (FR-OB-DASH-01·05), SLO 지표 (FR-OB-DASH-03)."""
from __future__ import annotations

import json
from typing import Any

from units.ob.ch import ClickHouse
from units.ob.dsl import compile_query

GW_CHAT = "ServiceName = 'atlas-gateway' AND startsWith(SpanName, 'chat ')"
GROUPS = {"model": "SpanAttributes['gen_ai.request.model']", "agent": "SpanAttributes['atlas.unit_id']",
          "tenant": "SpanAttributes['atlas.tenant']", "key": "SpanAttributes['atlas.key_prefix']",
          "provider": "SpanAttributes['gen_ai.provider.name']"}


async def search(ch: ClickHouse, q: str, since_s: int, limit: int) -> list[dict[str, Any]]:
    c = compile_query(q)
    sql = f"""
WITH ids AS (SELECT DISTINCT TraceId FROM otel_traces
             WHERE Timestamp >= now() - toIntervalSecond({{since:UInt32}}) AND ({c.where}) LIMIT 2000)
SELECT TraceId AS trace_id, toString(min(Timestamp)) AS start, argMin(SpanName, Timestamp) AS root,
       argMin(ServiceName, Timestamp) AS service, count() AS spans,
       round((toUnixTimestamp64Nano(max(Timestamp)) + max(Duration) - toUnixTimestamp64Nano(min(Timestamp))) / 1e6, 2) AS duration_ms,
       countIf(StatusCode = 'Error') AS errors,
       round(sum(toFloat64OrZero(SpanAttributes['atlas.cost.usd'])), 8) AS cost_usd,
       arrayFilter(x -> x != '', groupUniqArray(SpanAttributes['gen_ai.request.model'])) AS models,
       arrayFilter(x -> x != '', groupUniqArray(ServiceName)) AS services
FROM otel_traces WHERE TraceId IN ids GROUP BY TraceId ORDER BY start DESC LIMIT {{limit:UInt32}}"""
    return await ch.query(sql, {**c.params, "since": since_s, "limit": limit})


async def spans(ch: ClickHouse, trace_id: str) -> list[dict[str, Any]]:
    sql = """SELECT SpanId AS span_id, ParentSpanId AS parent_span_id, SpanName AS name, ServiceName AS service, SpanKind AS kind,
       toUnixTimestamp64Nano(Timestamp) AS start_ns, Duration AS duration_ns, StatusCode AS status, StatusMessage AS status_message,
       SpanAttributes AS attributes, Events.Name AS event_names, Events.Attributes AS event_attributes
FROM otel_traces WHERE TraceId = {t:String} ORDER BY Timestamp"""
    return await ch.query(sql, {"t": trace_id})


def tree(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """span 트리·워터폴·에이전트 위임 그래프 3뷰 (FR-OB-TRACE-03)."""
    if not rows:
        return {"spans": 0, "roots": [], "waterfall": [], "graph": []}
    t0 = min(int(r["start_ns"]) for r in rows)
    by = {r["span_id"]: {**r, "children": [], "offset_ms": round((int(r["start_ns"]) - t0) / 1e6, 2),
                         "duration_ms": round(int(r["duration_ns"]) / 1e6, 2),
                         "events": [{"name": n, "attributes": a} for n, a in zip(r.pop("event_names"), r.pop("event_attributes"), strict=True)]}
          for r in rows}
    roots, edges = [], {}
    for node in by.values():
        parent = by.get(node["parent_span_id"])
        if parent:
            parent["children"].append(node)
            if parent["service"] != node["service"]:
                k = (parent["service"], node["service"])
                edges[k] = edges.get(k, 0) + 1
        else:
            roots.append(node)
    waterfall = [{"span_id": n["span_id"], "name": n["name"], "service": n["service"], "offset_ms": n["offset_ms"],
                  "duration_ms": n["duration_ms"], "status": n["status"]} for n in sorted(by.values(), key=lambda x: x["offset_ms"])]
    return {"spans": len(rows), "roots": roots, "waterfall": waterfall,
            "graph": [{"from": a, "to": b, "calls": c} for (a, b), c in sorted(edges.items())]}


async def session(ch: ClickHouse, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    sql = """SELECT TraceId AS trace_id, toString(min(Timestamp)) AS start, argMin(SpanName, Timestamp) AS root, count() AS spans
FROM otel_traces WHERE TraceId IN (SELECT DISTINCT TraceId FROM otel_traces WHERE SpanAttributes['a2a.context.id'] = {s:String}
   OR SpanAttributes['gen_ai.conversation.id'] = {s:String} OR SpanAttributes['session.id'] = {s:String})
GROUP BY TraceId ORDER BY start LIMIT {limit:UInt32}"""
    return await ch.query(sql, {"s": session_id, "limit": limit})


async def llm_dashboard(ch: ClickHouse, since_s: int, group_by: str | None) -> dict[str, Any]:
    g = GROUPS.get(group_by or "", "'all'")
    base = f"FROM otel_traces WHERE {GW_CHAT} AND Timestamp >= now() - toIntervalSecond({{since:UInt32}})"
    summary = await ch.query(f"""SELECT {g} AS grp, count() AS requests, countIf(StatusCode = 'Error') AS errors,
       round(errors / greatest(requests, 1), 4) AS error_rate,
       round(quantile(0.5)(Duration) / 1e6, 2) AS p50_ms, round(quantile(0.95)(Duration) / 1e6, 2) AS p95_ms,
       sum(toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens'])) AS input_tokens,
       sum(toUInt64OrZero(SpanAttributes['gen_ai.usage.output_tokens'])) AS output_tokens,
       round(sum(toFloat64OrZero(SpanAttributes['atlas.cost.usd'])), 8) AS cost_usd,
       round(requests / ({{since:UInt32}} / 60), 3) AS rpm,
       countIf(SpanAttributes['guard.input.decision'] = 'block') AS guard_blocked
{base} GROUP BY grp ORDER BY requests DESC""", {"since": since_s})
    series = await ch.query(f"""SELECT toString(toStartOfMinute(Timestamp)) AS minute, count() AS requests,
       round(quantile(0.95)(Duration) / 1e6, 2) AS p95_ms, round(sum(toFloat64OrZero(SpanAttributes['atlas.cost.usd'])), 8) AS cost_usd
{base} GROUP BY minute ORDER BY minute""", {"since": since_s})
    return {"since_s": since_s, "group_by": group_by or "all", "summary": summary, "series": series,
            "notes": {"ttft": "스트림 업스트림 중계 전이라 TTFT 미측정", "cache_hit_rate": "EC-CACHE(M2) 전 미측정"}}


async def costs(ch: ClickHouse, since_s: int, group_by: str) -> list[dict[str, Any]]:
    g = GROUPS.get(group_by)
    if g is None:
        raise ValueError(f"group_by: {', '.join(GROUPS)}")
    return await ch.query(f"""SELECT {g} AS grp, count() AS requests,
       sum(toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens'])) AS input_tokens,
       sum(toUInt64OrZero(SpanAttributes['gen_ai.usage.output_tokens'])) AS output_tokens,
       round(sum(toFloat64OrZero(SpanAttributes['atlas.cost.usd'])), 8) AS cost_usd,
       round(cost_usd / greatest(requests, 1), 8) AS cost_per_request
FROM otel_traces WHERE {GW_CHAT} AND Timestamp >= now() - toIntervalSecond({{since:UInt32}})
GROUP BY grp ORDER BY cost_usd DESC""", {"since": since_s})


async def sli_counts(ch: ClickHouse, kind: str, threshold_ms: float | None, filt: str, window_s: int) -> tuple[int, int]:
    c = compile_query(filt, prefix="f")
    bad = "Duration / 1e6 > {th:Float64}" if kind == "latency" else "StatusCode = 'Error'"
    rows = await ch.query(f"""SELECT count() AS total, countIf({bad}) AS bad FROM otel_traces
WHERE Timestamp >= now() - toIntervalSecond({{w:UInt32}}) AND ({c.where})""", {**c.params, "w": window_s, "th": threshold_ms or 0})
    return int(rows[0]["total"]), int(rows[0]["bad"])


async def bad_examples(ch: ClickHouse, kind: str, threshold_ms: float | None, filt: str, window_s: int, n: int = 5) -> list[str]:
    c = compile_query(filt, prefix="f")
    bad = "Duration / 1e6 > {th:Float64}" if kind == "latency" else "StatusCode = 'Error'"
    rows = await ch.query(f"""SELECT DISTINCT TraceId AS t FROM otel_traces WHERE Timestamp >= now() - toIntervalSecond({{w:UInt32}})
AND ({c.where}) AND {bad} LIMIT {{n:UInt32}}""", {**c.params, "w": window_s, "th": threshold_ms or 0, "n": n})
    return [r["t"] for r in rows]


def trace_io(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """골든셋 승격용 — 첫 chat span 입력의 마지막 user 메시지, 마지막 chat span 출력, 도구 결과 (FR-EV-GOLD-03)."""
    chats = [r for r in rows if r["name"].startswith("chat ") and r["service"] == "atlas-gateway"]
    first_in = json.loads(chats[0]["attributes"].get("gen_ai.input.messages", "[]")) if chats else []
    last_out = json.loads(chats[-1]["attributes"].get("gen_ai.output.messages", "[]")) if chats else []
    user = next((m.get("content", "") for m in reversed(first_in) if m.get("role") == "user"), "")
    output = next((m.get("content", "") for m in reversed(last_out) if m.get("role") == "assistant"), "")
    tools = [m.get("content", "") for c in chats for m in json.loads(c["attributes"].get("gen_ai.input.messages", "[]")) if m.get("role") == "tool"]
    return {"input": user, "output": output, "tool_results": tools, "model": chats[0]["attributes"].get("gen_ai.request.model") if chats else None,
            "captured": bool(first_in)}
