// 01 Agentic Loops — 런 목록·시작·이벤트 타임라인·HITL 승인 (AL-RUN via Manager agent-proxy)
import { useState } from "react";
import { fetchAgents, sendA2A, svcGet } from "../lib/api";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type RunRow = { id: string; goal: string; status: string; reason: string | null; turns: number; updated_at: string };
type Ev = { type: string; data: Record<string, unknown> };

async function events(id: string, tenant: string): Promise<Ev[]> {
  const res = await fetch(`/svc/alrun/runs/${id}/events?follow=false&tenant=${encodeURIComponent(tenant)}`);
  const out: Ev[] = [];
  let type = "";
  for (const line of (await res.text()).split("\n")) {
    if (line.startsWith("event: ")) type = line.slice(7);
    else if (line.startsWith("data: ")) out.push({ type, data: JSON.parse(line.slice(6)) });
  }
  return out;
}

export function Runs({ route, open, confirm }: { route: Route; open: (id?: string) => void; confirm: (text: string, then: () => void) => void }) {
  const tenant = route.org;
  const list = useData(() => svcGet<{ items: RunRow[] }>("alrun", "/runs", { tenant, limit: 30 }), [tenant], 4000);
  const detail = useData(async () => (route.id ? { run: await svcGet<{ status: string; reason: string; result: Record<string, unknown> }>("alrun", `/runs/${route.id}`, { tenant }), evs: await events(route.id, tenant) } : null), [route.id, tenant], 3000);
  const [goal, setGoal] = useState('atlas_calc({"expr":"6*7"}) 로 계산해 답한다');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function call(data: Record<string, unknown>, taskId?: string) {
    setBusy(true); setErr(null);
    try {
      const agents = await fetchAgents();
      const alrun = agents.find((a) => a.unitId === "AL-RUN");
      if (!alrun) throw new Error("AL-RUN 미등록");
      const body = { jsonrpc: "2.0", id: 1, method: "SendMessage",
        params: { tenant, message: { role: "ROLE_USER", messageId: crypto.randomUUID(), parts: [{ data }], ...(taskId ? { taskId } : {}) } } };
      const out = (await sendA2A(alrun.id, body)) as { result?: { task: { id: string } }; error?: { message: string } };
      if (out.error) throw new Error(out.error.message);
      open(out.result!.task.id);
      list.reload(); detail.reload();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  const pending = detail.data?.run.status === "pending";

  return (
    <div className="grid2">
      <Panel title="루프 런" sub="Plan → Act → Observe → Reflect → Exit (AL-RUN)">
        <label htmlFor="goal">새 목표</label>
        <textarea id="goal" rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} />
        <button type="button" disabled={busy || !goal.trim()} onClick={() => confirm("prod 에서 루프를 실행합니다.", () => call({ goal }))}>{busy ? "실행 중…" : "실행"}</button>
        <ErrorLine error={err ?? list.error} />
        {!list.data?.items.length ? <Empty>런이 없습니다.</Empty> : (
          <ul className="list">{list.data.items.map((r) => (
            <li key={r.id}><button type="button" className={`link${r.id === route.id ? " active" : ""}`} onClick={() => open(r.id)}>
              <span className={`badge b-${r.status}`}>{r.status}</span> {r.goal} <small>{r.reason ?? ""} · {r.turns}턴 · {fmtTime(r.updated_at)}</small>
            </button></li>))}</ul>
        )}
      </Panel>
      <Panel title="런 상세" sub={route.id ?? "런을 선택하세요"}>
        <ErrorLine error={detail.error} />
        {detail.data && (
          <>
            <p><span className={`badge b-${detail.data.run.status}`}>{detail.data.run.status}</span> <b>{detail.data.run.reason}</b></p>
            {pending && (
              <div className="row hitl" role="group" aria-label="사람 승인">
                <span>승인 대기: {JSON.stringify((detail.data.run.result as { pending_approval?: unknown })?.pending_approval ?? [])}</span>
                <button type="button" className="danger" disabled={busy} onClick={() => confirm("prod 에서 비가역 도구 실행을 승인합니다.", () => call({ approval: "approve" }, route.id))}>승인</button>
                <button type="button" disabled={busy} onClick={() => call({ approval: "deny" }, route.id)}>거부</button>
              </div>
            )}
            <ol className="timeline">{detail.data.evs.map((e, i) => (
              <li key={i} className={`ev-${e.type}`}><code>{e.type}</code> <small>{JSON.stringify(e.data).slice(0, 160)}</small></li>))}</ol>
            <pre className="result">{JSON.stringify(detail.data.run.result, null, 2)}</pre>
          </>
        )}
      </Panel>
    </div>
  );
}
