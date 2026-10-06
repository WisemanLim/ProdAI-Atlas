// 08 Observability — trace 검색(DSL)·워터폴·대시보드 요약 (OB)
import { useState } from "react";
import { svcGet } from "../lib/api";
import type { Route } from "../lib/router";
import { layout, type TreeNode, type WSpan } from "../lib/waterfall";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type TraceRow = { trace_id: string; start: string; root: string; services: string[]; spans: number; duration_ms: number; errors: number; cost_usd: number; models: string[] };
type Tree = { waterfall: WSpan[]; roots: TreeNode[]; graph: { from: string; to: string; calls: number }[] };
type Dash = { summary: { grp: string; requests: number; error_rate: number; p50_ms: number; p95_ms: number; cost_usd: number; rpm: number }[] };

export function Traces({ route, open }: { route: Route; open: (id?: string) => void }) {
  const [q, setQ] = useState('service = "atlas-gateway"');
  const [query, setQuery] = useState(q);
  const list = useData(() => svcGet<{ items: TraceRow[] }>("ob", "/v1/traces", { q: query, since: 3600, limit: 30 }), [query], 10000);
  const dash = useData(() => svcGet<Dash>("ob", "/v1/dashboards/llm", { since: 3600, group_by: "model" }), [], 15000);
  const tree = useData(() => (route.id ? svcGet<Tree>("ob", `/v1/traces/${route.id}`) : Promise.resolve(null)), [route.id]);
  const bars = tree.data ? layout(tree.data.waterfall, tree.data.roots) : [];
  return (
    <>
      <Panel title="LLM 대시보드 (1h)" sub="Gateway chat span 기준 — 모델별">
        <ErrorLine error={dash.error} />
        <div className="kpis">{dash.data?.summary.map((s) => (
          <div key={s.grp} className="kpi"><b>{s.grp || "—"}</b><span>{s.requests} req · p95 {s.p95_ms}ms · 오류 {(s.error_rate * 100).toFixed(1)}% · ${s.cost_usd}</span></div>))}
          {dash.data && dash.data.summary.length === 0 && <Empty>최근 1시간 호출 없음</Empty>}</div>
      </Panel>
      <div className="grid2">
        <Panel title="trace 검색" sub='DSL: key op value AND … (op = != > >= < <= ~)'>
          <form className="row" onSubmit={(e) => { e.preventDefault(); setQuery(q); }}>
            <input aria-label="검색 DSL" value={q} onChange={(e) => setQ(e.target.value)} />
            <button type="submit">검색</button>
          </form>
          <ErrorLine error={list.error} />
          {!list.data?.items.length ? <Empty>결과 없음</Empty> : (
            <ul className="list">{list.data.items.map((t) => (
              <li key={t.trace_id}><button type="button" className={`link${t.trace_id === route.id ? " active" : ""}`} onClick={() => open(t.trace_id)}>
                {t.errors > 0 && <span className="badge b-failed">error</span>} <code>{t.root}</code> <small>{t.services.join(" · ")} · {t.spans} spans · {t.duration_ms}ms · ${t.cost_usd} · {fmtTime(t.start)}</small>
              </button></li>))}</ul>
          )}
        </Panel>
        <Panel title="워터폴" sub={route.id ?? "trace 를 선택하세요"}>
          <ErrorLine error={tree.error} />
          {tree.data && (
            <>
              <div className="waterfall" role="list">{bars.map((b) => (
                <div key={b.span_id} className="wf-row" role="listitem" title={`${b.service} · ${b.duration_ms}ms`}>
                  <span className="wf-name" style={{ paddingLeft: `${b.depth * 12}px` }}>{b.name}</span>
                  <span className="wf-track"><span className={`wf-bar${b.status === "Error" ? " err" : ""}`} style={{ left: `${b.left}%`, width: `${b.width}%` }} /></span>
                  <small>{b.duration_ms}ms</small>
                </div>))}</div>
              <p className="msg">위임: {tree.data.graph.map((g) => `${g.from} → ${g.to} (${g.calls})`).join(" · ") || "—"}</p>
            </>
          )}
        </Panel>
      </div>
    </>
  );
}
