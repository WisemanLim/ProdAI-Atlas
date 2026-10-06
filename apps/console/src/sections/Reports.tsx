// S102 사용량 리포트 — 기간·차원별 비용(통화·CSV/PDF) · 통합 감사 검색·체인/WORM 검증 · 이상 사용 · SIEM 싱크 · 월간 보고서 (FR-X-RPT-01~06·08)
import { useState } from "react";
import { svcGet, svcUrl } from "../lib/api";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Row = Record<string, string | number>;
type Usage = { items: Row[]; total: number; currency: string; group_by: string[] };
type Audit = { seq: number; ts: string; actor: string; actor_type: string; action: string; resource: string; result: string };
type Verify = { events: number; valid: boolean; broken_at: number | null; worm: { segments: number; match: boolean }; retention_days: number };
type Anomaly = { id: string; agent_id: string; meter: string; window_start: string; value: number; mean: number; z: number; traces: string[] };
type Sink = { id: string; type: string; buffered: number; sent: number; failures: number; last_error: string | null };
type Report = { id: string; month: string; project: string; summary: { cost_usd: number; requests: number } };

const GROUPS = ["project,model", "agent", "model", "unit", "day", "meter"];
const CUR = ["USD", "KRW", "EUR", "JPY"];

export function Reports({ route }: { route: Route }) {
  const [gb, setGb] = useState(GROUPS[0]);
  const [cur, setCur] = useState("USD");
  const [days, setDays] = useState(30);
  const since = new Date(Date.now() - days * 86400_000).toISOString();
  const params = { tenant: route.org, group_by: gb, currency: cur, since };
  const usage = useData(() => svcGet<Usage>("rpt", "/v1/reports/usage", params), [route.org, gb, cur, days], 30000);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const audit = useData(() => svcGet<{ items: Audit[] }>("rpt", "/v1/audit", { tenant: route.org, action, actor, limit: 50 }), [route.org, action, actor], 15000);
  const verify = useData(() => svcGet<Verify>("rpt", "/v1/audit/verify"), [route.org], 60000);
  const anomalies = useData(() => svcGet<{ items: Anomaly[] }>("rpt", "/v1/anomalies", { tenant: route.org }), [route.org], 30000);
  const sinks = useData(() => svcGet<{ items: Sink[] }>("rpt", "/v1/siem/sinks"), [route.org], 15000);
  const reports = useData(() => svcGet<{ items: Report[] }>("rpt", "/v1/reports", { tenant: route.org }), [route.org]);
  const u = usage.data;
  const cols = u ? [...u.group_by, "cost", "requests", "tokens_in", "tokens_out", "tool_calls", "sbx_cpu_s"] : [];
  return (
    <>
      <Panel title="비용 리포트" sub="기간·차원별 그룹 — 원천 GW-COST·X-SBX·직접 수집" actions={<>
        <select aria-label="그룹" value={gb} onChange={(e) => setGb(e.target.value)}>{GROUPS.map((g) => <option key={g}>{g}</option>)}</select>
        <select aria-label="통화" value={cur} onChange={(e) => setCur(e.target.value)}>{CUR.map((c) => <option key={c}>{c}</option>)}</select>
        <select aria-label="기간" value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 30, 90].map((d) => <option key={d} value={d}>{d}일</option>)}</select>
        <a href={svcUrl("rpt", "/v1/reports/usage", { ...params, format: "csv" })}>CSV</a>
        <a href={svcUrl("rpt", "/v1/reports/usage", { ...params, format: "pdf" })} target="_blank" rel="noreferrer">PDF</a></>}>
        <ErrorLine error={usage.error} />
        {!u?.items.length ? <Empty>사용량 없음</Empty> : <>
          <p className="meta">합계 <b>{u.total.toLocaleString()} {u.currency}</b></p>
          <div className="table-wrap"><table aria-label="사용량"><thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{u.items.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{String(r[c] ?? "")}</td>)}</tr>)}</tbody></table></div></>}
      </Panel>
      <div className="grid2">
        <Panel title="감사 로그" sub={verify.data ? `체인 ${verify.data.valid ? "정상" : `손상 @${verify.data.broken_at}`} · WORM ${verify.data.worm.match ? "일치" : "불일치"} · ${verify.data.events}건 · 보존 ${verify.data.retention_days}일` : "검증 중…"}
          actions={<><input aria-label="행위 접두어" placeholder="행위 (agent. · plt.mkt.)" value={action} onChange={(e) => setAction(e.target.value)} />
            <input aria-label="행위자" placeholder="행위자" value={actor} onChange={(e) => setActor(e.target.value)} /></>}>
          <ErrorLine error={audit.error ?? verify.error} />
          {!audit.data?.items.length ? <Empty>감사 기록 없음</Empty> : (
            <div className="table-wrap"><table aria-label="감사"><thead><tr><th>#</th><th>시각</th><th>행위자</th><th>행위</th><th>자원</th><th>결과</th></tr></thead>
              <tbody>{audit.data.items.map((a) => <tr key={a.seq}><td>{a.seq}</td><td>{fmtTime(a.ts)}</td><td>{a.actor} <small>{a.actor_type}</small></td>
                <td><code>{a.action}</code></td><td><small>{a.resource}</small></td><td>{a.result}</td></tr>)}</tbody></table></div>)}
        </Panel>
        <Panel title="이상 사용" sub="에이전트별 5분 창 EWMA · z > 3">
          <ErrorLine error={anomalies.error} />
          {!anomalies.data?.items.length ? <Empty>탐지된 이상 없음</Empty> : (
            <ul className="list">{anomalies.data.items.map((a) => <li key={a.id}><b>{a.agent_id}</b> {a.meter} {a.value} (평균 {a.mean}, z={a.z}) <small>{fmtTime(a.window_start)}</small>
              {a.traces.slice(0, 3).map((t) => <a key={t} href={svcUrl("ob", "/v1/traces", { request_id: t })}> {t.slice(0, 8)}</a>)}</li>)}</ul>)}
          <h3>SIEM 싱크</h3>
          {!sinks.data?.items.length ? <Empty>싱크 없음</Empty> : (
            <ul className="list">{sinks.data.items.map((s) => <li key={s.id}><b>{s.id}</b> {s.type} · 전송 {s.sent} · 버퍼 {s.buffered}{s.last_error && <span className="r-fail"> {s.last_error}</span>}</li>)}</ul>)}
          <h3>월간 보고서</h3>
          {!reports.data?.items.length ? <Empty>보고서 없음</Empty> : (
            <ul className="list">{reports.data.items.map((r) => <li key={r.id}>{r.month} {r.project} · ${r.summary.cost_usd}{" "}
              <a href={svcUrl("rpt", `/v1/reports/${r.id}.html`)} target="_blank" rel="noreferrer">HTML</a> <a href={svcUrl("rpt", `/v1/reports/${r.id}.pdf`)}>PDF</a></li>)}</ul>)}
        </Panel>
      </div>
    </>
  );
}
