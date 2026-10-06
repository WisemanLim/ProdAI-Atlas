// 06 Evals — 골든셋·검수 대기열·회귀 실행 (EV)
import { svcGet } from "../lib/api";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Dataset = { id: string; name: string; description: string; counts: Record<string, number> };
type Item = { id: string; input: unknown; source: string; source_trace_id: string | null; tags: string[] };
type Judge = { name: string; version: number; mode: string; model: string; jury: string[]; calibration: { kappa: number; trusted: boolean } | null };
type Card = { target: string; suite: string; task_success_rate: number | null; pass_hat_k: number | null; cost_per_task_usd: number; p95_latency_ms: number };
type Run = { run_id: string; suite: string; status: string; gate: string | null; dataset_version: number; pass_rate: number | null; created_at: string };

export function Evals() {
  const ds = useData(() => svcGet<{ items: Dataset[] }>("ev", "/v1/datasets"), [], 10000);
  const queue = useData(async () => {
    const sets = (await svcGet<{ items: Dataset[] }>("ev", "/v1/datasets")).items;
    const lists = await Promise.all(sets.map((d) => svcGet<{ items: Item[] }>("ev", `/v1/datasets/${d.name}/items`, { status: "in_review" }).then((r) => r.items.map((i) => ({ ...i, dataset: d.name })))));
    return lists.flat();
  }, [], 10000);
  const runs = useData(() => svcGet<{ items: Run[] }>("ev", "/v1/runs", { limit: 20 }), [], 10000);
  const judges = useData(() => svcGet<{ items: Judge[] }>("ev", "/v1/judges"), [], 15000);
  const card = useData(() => svcGet<{ items: Card[] }>("ev", "/v1/scorecard"), [], 15000);
  return (
    <>
      <div className="grid2">
        <Panel title="골든셋" sub="draft → in_review → approved → retired · 승인 항목만 버전 스냅샷">
          <ErrorLine error={ds.error} />
          {!ds.data?.items.length ? <Empty>데이터셋 없음 — <code>make eval-seed</code></Empty> : (
            <div className="table-wrap"><table><thead><tr><th>이름</th><th>draft</th><th>검수 대기</th><th>승인</th></tr></thead>
              <tbody>{ds.data.items.map((d) => <tr key={d.id}><td><code>{d.name}</code></td><td>{d.counts.draft}</td><td>{d.counts.in_review}</td><td>{d.counts.approved}</td></tr>)}</tbody></table></div>
          )}
        </Panel>
        <Panel title="검수 대기열" sub="trace 승격 등 — 승인은 큐레이터 권한(API)">
          <ErrorLine error={queue.error} />
          {!queue.data?.length ? <Empty>대기 항목 없음</Empty> : (
            <ul className="list">{queue.data.map((i) => (
              <li key={i.id}><span className="badge b-in_review">{i.dataset}</span> <code>{JSON.stringify(i.input).slice(0, 80)}</code>
                <small> {i.source}{i.source_trace_id ? ` · trace ${i.source_trace_id.slice(0, 8)}` : ""}</small></li>))}</ul>
          )}
        </Panel>
      </div>
      <div className="grid2">
        <Panel title="judge" sub="루브릭 버전 · 사람 일치도 κ (미보정 judge 는 게이트 불가)">
          {!judges.data?.items.length ? <Empty>judge 없음</Empty> : (
            <table aria-label="judge"><thead><tr><th>이름</th><th>버전</th><th>모드</th><th>모델</th><th>κ</th></tr></thead>
              <tbody>{judges.data.items.map((j) => <tr key={`${j.name}${j.version}`}><td><code>{j.name}</code></td><td>v{j.version}</td><td>{j.mode}</td>
                <td>{j.jury.length ? `배심원 ${j.jury.length}` : j.model}</td><td><span className={`badge b-${j.calibration?.trusted ? "allow" : "pending"}`}>{j.calibration?.kappa ?? "미보정"}</span></td></tr>)}</tbody></table>)}
        </Panel>
        <Panel title="스코어카드" sub="타깃(에이전트·프롬프트 버전)별 최신 실행">
          {!card.data?.items.length ? <Empty>실행 없음</Empty> : (
            <table aria-label="스코어카드"><thead><tr><th>타깃</th><th>성공률</th><th>pass^k</th><th>과업당 비용</th><th>p95</th></tr></thead>
              <tbody>{card.data.items.map((x) => <tr key={x.target}><td>{x.target}<br /><small>{x.suite}</small></td><td>{x.task_success_rate}</td><td>{x.pass_hat_k}</td>
                <td>{x.cost_per_task_usd}</td><td>{x.p95_latency_ms}ms</td></tr>)}</tbody></table>)}
        </Panel>
      </div>
      <Panel title="회귀 실행" sub="eval-as-code 스위트 — gate pass|fail (CI: exit 0/100/2)">
        <ErrorLine error={runs.error} />
        {!runs.data?.items.length ? <Empty>실행 없음 — <code>make eval</code></Empty> : (
          <div className="table-wrap"><table><thead><tr><th>시각</th><th>스위트</th><th>버전</th><th>pass_rate</th><th>게이트</th></tr></thead>
            <tbody>{runs.data.items.map((r) => <tr key={r.run_id}><td>{fmtTime(r.created_at)}</td><td>{r.suite}</td><td>v{r.dataset_version}</td><td>{r.pass_rate ?? "—"}</td>
              <td><span className={`badge b-${r.gate ?? r.status}`}>{r.gate ?? r.status}</span></td></tr>)}</tbody></table></div>
        )}
      </Panel>
    </>
  );
}
