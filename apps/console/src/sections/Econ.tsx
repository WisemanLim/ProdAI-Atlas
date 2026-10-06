// 05 Inference Economics — 비용 시뮬레이터 (FR-EC-CTRL-06): 레버 워터폴(캐시 → 배치 → 라우팅 → 모델)·민감도
import { useState } from "react";
import { svcPost } from "../lib/api";
import { ErrorLine, Panel } from "../ui";

type Step = { lever: string; per_task_usd: number; monthly_usd: number; savings_monthly_usd: number; savings_pct: number; description: string };
type SimOut = { price_as_of: string; input_tokens_per_task: number; output_tokens_per_task: number; waterfall: Step[]; final_monthly_usd: number;
  total_saving_pct: number; sensitivity: Record<string, { value: number; monthly_usd: number }[]> };

const usd = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: v < 1 ? 4 : 0 })}`;

export function Econ() {
  const [f, setF] = useState({ tokens_per_task: 50000, input_ratio: 0.8, cache_ratio: 0.6, batch_ratio: 0.3, route_ratio: 0.4, tasks_per_day: 1000,
    model: "mock-chat-large", route_model: "mock-chat-small", alt_model: "mock-chat" });
  const [out, setOut] = useState<SimOut | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const num = (k: keyof typeof f, step = 0.05) => (
    <label key={k}>{k}<input type="number" step={step} value={f[k] as number} onChange={(e) => setF({ ...f, [k]: Number(e.target.value) })} /></label>
  );
  const max = out ? Math.max(...out.waterfall.map((s) => s.monthly_usd)) : 1;
  return (
    <div className="grid2">
      <Panel title="비용 시뮬레이터" sub="50,000 tokens/task 가 청구서를 바꾼다 — 가격표 버전 고정">
        <div className="form-grid">
          {num("tokens_per_task", 1000)}{num("input_ratio")}{num("cache_ratio")}{num("batch_ratio")}{num("route_ratio")}{num("tasks_per_day", 100)}
          {(["model", "route_model", "alt_model"] as const).map((k) => (
            <label key={k}>{k}<input value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>))}
        </div>
        <button type="button" onClick={() => svcPost<SimOut>("gw", "/v1/econ/simulate", f).then((o) => { setOut(o); setErr(null); }).catch((e: Error) => setErr(e.message))}>계산</button>
        <ErrorLine error={err} />
      </Panel>
      <Panel title="절감 워터폴" sub={out ? `가격 기준 ${out.price_as_of} · 입력 ${out.input_tokens_per_task.toLocaleString()} / 출력 ${out.output_tokens_per_task.toLocaleString()} 토큰` : "계산을 누르세요"}>
        {out && <>
          <div className="table-wrap"><table aria-label="워터폴"><thead><tr><th>레버</th><th>월 비용</th><th>절감</th><th /></tr></thead>
            <tbody>{out.waterfall.map((s) => <tr key={s.lever}><td title={s.description}>{s.lever}</td><td>{usd(s.monthly_usd)}</td>
              <td>{s.savings_monthly_usd ? `−${usd(s.savings_monthly_usd)} (${s.savings_pct}%)` : ""}</td>
              <td><div className="bar" style={{ width: `${Math.max(2, (s.monthly_usd / max) * 100)}%` }} /></td></tr>)}</tbody></table></div>
          <p className="msg">최종 월 {usd(out.final_monthly_usd)} · 총 절감 {out.total_saving_pct}%</p>
          <p className="msg">민감도(cache_ratio): {out.sensitivity.cache_ratio.map((r) => `${r.value}→${usd(r.monthly_usd)}`).join(" · ")}</p>
        </>}
      </Panel>
    </div>
  );
}
