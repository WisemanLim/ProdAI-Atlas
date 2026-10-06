// 09 Bitter Lesson — 모델 후보·카나리·alias·수명종료 · 프롬프트 레지스트리 · 보정 플래그 (BL-MODEL·HARN)
import { useState } from "react";
import { svcGet, svcPost } from "../lib/api";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Cand = { id: string; alias: string; base_model: string; model: string; status: string; decision: string | null; updated_at: string;
  detail: { metrics?: Record<string, number>; canary?: { stages: number[]; stage: number } } };
type Alias = { name: string; version: number; source: string; deployments: { model: string; weight: number }[]; fallbacks: string[] };
type Art = { name: string; version: number; labels: string[]; created_at: string };
type Flag = { key: string; enabled: boolean; reason: string; introduced_for: string; removal_condition: string };

export function BitterLesson({ confirm }: { confirm: (text: string, then: () => void) => void }) {
  const cands = useData(() => svcGet<{ items: Cand[] }>("bl", "/v1/candidates"), [], 10000);
  const aliases = useData(() => svcGet<{ items: Alias[] }>("bl", "/v1/aliases"), [], 10000);
  const eol = useData(() => svcGet<{ items: { model: string; days_left: number; aliases: string[]; replacement: string | null }[] }>("bl", "/v1/models/eol"), []);
  const prompts = useData(() => svcGet<{ items: Art[] }>("bl", "/v1/prompts"), [], 10000);
  const flags = useData(() => svcGet<{ items: Flag[] }>("bl", "/v1/flags"), [], 10000);
  const [msg, setMsg] = useState<string | null>(null);
  const act = (cid: string, path: string, label: string) => confirm(`${label} — 운영 트래픽 라우팅이 바뀝니다.`, () =>
    svcPost<Record<string, unknown>>("bl", `/v1/candidates/${cid}/${path}`, path === "canary" ? { min_samples: 50 } : { by: "console" })
      .then((r) => { setMsg(`${label}: ${String(r.action ?? r.status ?? "ok")}`); cands.reload(); aliases.reload(); }).catch((e: Error) => setMsg(e.message)));
  return (
    <>
      <ErrorLine error={cands.error ?? msg} />
      <Panel title="모델 후보" sub="0% 등록 → 회귀 평가(pass^k) → 채택 규칙 → 카나리 → 전환(기존 모델 폴백)">
        {!cands.data?.items.length ? <Empty>후보 없음 — <code>POST /v1/models/{"{model}"}/candidate</code></Empty> : (
          <div className="table-wrap"><table><thead><tr><th>alias</th><th>기준 → 후보</th><th>판정</th><th>상태</th><th>pass^k</th><th>과업당 비용</th><th /></tr></thead>
            <tbody>{cands.data.items.map((c) => { const m = c.detail.metrics ?? {}; return (
              <tr key={c.id}><td>{c.alias}</td><td>{c.base_model} → <b>{c.model}</b></td><td><span className={`badge b-${c.decision === "promote" ? "allow" : "pending"}`}>{c.decision ?? "-"}</span></td>
                <td>{c.status}{c.detail.canary ? ` ${c.detail.canary.stages[c.detail.canary.stage]}%` : ""}</td>
                <td>{m.base_pass_hat_k ?? "-"} → {m.cand_pass_hat_k ?? "-"}</td><td>{m.base_cost_per_case ?? "-"} → {m.cand_cost_per_case ?? "-"}</td>
                <td>{c.status === "decided" && <button type="button" onClick={() => act(c.id, "canary", "카나리 시작")}>카나리</button>}
                  {c.status === "canary" && <button type="button" onClick={() => act(c.id, "canary/advance", "카나리 진행")}>진행</button>}
                  {["decided", "canary", "promoted"].includes(c.status) && <button type="button" className="danger" onClick={() => act(c.id, "rollback", "롤백")}>롤백</button>}</td></tr>); })}</tbody></table></div>)}
      </Panel>
      <div className="grid2">
        <Panel title="alias" sub="런타임 오버레이 · 버전 · 가중치(0 = 트래픽 없음)">
          <table><tbody>{aliases.data?.items.filter((a) => a.source === "overlay" || a.deployments.length > 1).map((a) => (
            <tr key={a.name}><td><code>{a.name}</code> v{a.version}</td><td>{a.deployments.map((d) => `${d.model}:${d.weight}`).join(" · ")}</td><td>{a.fallbacks.join(", ")}</td></tr>))}</tbody></table>
          {eol.data?.items.map((e) => <p key={e.model} className="alert">수명종료 {e.days_left}일 전: {e.model} ({e.aliases.join(", ")}) → 대체 {e.replacement ?? "없음"}</p>)}
        </Panel>
        <Panel title="프롬프트 레지스트리 · 보정 플래그" sub="production 라벨 = 게시 게이트 통과 · 플래그 = flagd">
          <table><tbody>{prompts.data?.items.map((p) => <tr key={`${p.name}${p.version}`}><td><code>{p.name}</code></td><td>v{p.version}</td><td>{p.labels.join(", ")}</td><td>{fmtTime(p.created_at)}</td></tr>)}</tbody></table>
          <table aria-label="보정 플래그"><tbody>{flags.data?.items.map((f) => <tr key={f.key} title={`제거 조건: ${f.removal_condition}`}><td><code>{f.key}</code></td>
            <td>{f.enabled ? "on" : "off"}</td><td>{f.reason}</td><td><small>{f.introduced_for}</small></td></tr>)}</tbody></table>
        </Panel>
      </div>
    </>
  );
}
