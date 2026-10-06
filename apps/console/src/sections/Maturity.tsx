// MAT 성숙도 — 4속성 점수·단계 게이트·9개념 커버리지·복리 루프·90일 로드맵·공통 지표·스냅샷 (FR-MAT-01~06·08·10)
import { useState } from "react";
import { svcGet, svcPost } from "../lib/api";
import { scoreClass, type Band } from "../lib/maturity";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Check = { id: string; desc: string; weight: number; result: string };
type Gate = { id: string; task: string; role: string; result: string };
type Assessment = {
  band: Band; attributes: Record<string, { score: number; checks: Check[] }>;
  stage: { level: number; name: string; levels: { level: number; name: string; passed: boolean; gates: Gate[] }[] };
  coverage: { matrix: Record<string, Record<string, string>>; adopted: number; total: number };
  compound: Record<string, unknown>; roadmap: { cards: { title: string; gate: string; role: string; weeks: [number, number] }[] };
  metrics: { name: string; value: number | null; unit: string; definition: string }[];
  outcomes: { cards: { name: string; current: number | null; baseline: number | null; change_pct: number | null; improved: boolean | null }[] };
  source_errors: Record<string, string>;
};
const ATTR = { reliable: "Reliable", observable: "Observable", cost_aware: "Cost-aware", safe: "Safe" } as const;

export function Maturity({ route }: { route: Route }) {
  const q = { org: route.org, project: route.proj };
  const a = useData(() => svcGet<Assessment>("mat", "/v1/assessment", q), [route.org, route.proj], 30000);
  const snaps = useData(() => svcGet<{ items: { id: string; level: number; scores: Record<string, number>; kind: string; created_at: string }[] }>("mat", "/v1/snapshots", q), [route.org, route.proj]);
  const [err, setErr] = useState<string | null>(null);
  const d = a.data;
  return (
    <>
      <ErrorLine error={a.error ?? err} />
      {!d ? <Empty>진단 중…</Empty> : <>
        <div className="title-row"><h1>L{d.stage.level} {d.stage.name} <small>({d.band.phase})</small></h1>
          <div className="meta"><span>개념 도입 {d.coverage.adopted}/{d.coverage.total}</span>
            <a href={`/svc/mat/v1/report?org=${route.org}&project=${route.proj}`} target="_blank" rel="noreferrer">리포트(Markdown)</a></div></div>
        {Object.keys(d.source_errors).length > 0 && <p className="msg">수집 실패 출처(unknown 처리): {Object.keys(d.source_errors).join(", ")}</p>}
        <div className="grid2">
          <Panel title="4속성 점수" sub="체크 항목 × 가중치 · 텔레메트리 판정">
            {Object.entries(d.attributes).map(([k, v]) => (
              <details key={k}><summary>{ATTR[k as keyof typeof ATTR]} <b>{v.score}</b><div className="meter"><i className={scoreClass(v.score)} style={{ width: `${v.score}%` }} /></div></summary>
                <ul className="list">{v.checks.map((c) => <li key={c.id}><span className={`r-${c.result}`}>{c.result === "pass" ? "✓" : c.result === "fail" ? "✗" : "?"}</span> {c.desc} <small>×{c.weight}</small></li>)}</ul>
              </details>))}
          </Panel>
          <Panel title="단계 게이트" sub="게이트를 모두 충족해야 다음 단계">
            {d.stage.levels.map((lv) => (
              <div key={lv.level} className="subpanel"><b className={lv.passed ? "r-pass" : ""}>L{lv.level} {lv.name}</b>
                <ul className="list">{lv.gates.map((g) => <li key={g.id}><span className={`r-${g.result}`}>{g.result === "pass" ? "✓" : g.result === "fail" ? "✗" : "?"}</span> {g.task} <small>{g.role}</small></li>)}</ul></div>))}
          </Panel>
        </div>
        <div className="grid2">
          <Panel title="9개념 × 4속성 커버리지" sub="● 도입+속성 70점 · ◐ 도입 · 빈칸 미도입">
            <table aria-label="커버리지"><thead><tr><th>개념</th>{Object.values(ATTR).map((x) => <th key={x}>{x}</th>)}</tr></thead>
              <tbody>{Object.entries(d.coverage.matrix).map(([cpt, row]) => <tr key={cpt}><td>{cpt}</td>{Object.keys(ATTR).map((k) => <td key={k}>{row[k]}</td>)}</tr>)}</tbody></table>
          </Panel>
          <Panel title="90일 로드맵" sub="미충족 게이트 → 작업 카드 (역할별)">
            {!d.roadmap.cards.length ? <Empty>남은 게이트 없음</Empty> : (
              <table aria-label="로드맵"><thead><tr><th>주차</th><th>작업</th><th>역할</th></tr></thead>
                <tbody>{d.roadmap.cards.map((c) => <tr key={c.gate}><td>{c.weeks[0]}~{c.weeks[1]}</td><td>{c.title}</td><td>{c.role}</td></tr>)}</tbody></table>)}
          </Panel>
        </div>
        <div className="grid2">
          <Panel title="공통 지표" sub="대시보드·스코어카드·MAT 같은 정의">
            <table><tbody>{d.metrics.map((m) => <tr key={m.name} title={m.definition}><td><code>{m.name}</code></td><td>{m.value ?? "—"}</td><td>{m.unit}</td></tr>)}</tbody></table>
          </Panel>
          <Panel title="성과 · 복리 루프" sub="기준선(첫 스냅샷) 대비" actions={<button type="button" onClick={() => svcPost("mat", `/v1/snapshots?org=${route.org}&project=${route.proj}`, {})
            .then(() => { snaps.reload(); a.reload(); }).catch((e: Error) => setErr(e.message))}>스냅샷</button>}>
            <ul className="list">{d.outcomes.cards.map((c) => <li key={c.name}><b>{c.name}</b> {c.current ?? "—"} <small>기준 {c.baseline ?? "—"} · {c.change_pct ?? "—"}%</small></li>)}</ul>
            <pre className="result">{JSON.stringify(d.compound, null, 1)}</pre>
            <ul className="list">{snaps.data?.items.map((s) => <li key={s.id}>{fmtTime(s.created_at)} · L{s.level} · {s.kind}</li>)}</ul>
          </Panel>
        </div>
      </>}
    </>
  );
}
