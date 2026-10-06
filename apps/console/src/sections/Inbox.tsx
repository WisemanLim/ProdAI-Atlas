// X-HITL 통합 승인함 — 위험도·만료순, 승인·반려·수정 후 승인 (FR-X-HITL-01·02·07)
import { useState } from "react";
import { editDecision, mgr, untilText, type Approval } from "../lib/api";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

export function Inbox({ route, confirm }: { route: Route; confirm: (text: string, then: () => void) => void }) {
  const [status, setStatus] = useState("pending");
  const list = useData(() => mgr<{ items: Approval[] }>("GET", `/api/v1/approvals?org=${route.org}&status=${status}`), [route.org, status], 5000);
  const [sel, setSel] = useState<Approval | null>(null);
  const [reason, setReason] = useState("");
  const [args, setArgs] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const pick = (a: Approval) => {
    setSel(a); setReason(""); setErr(null);
    const p = JSON.parse(a.payload || "{}");
    setArgs(JSON.stringify(p.arguments ?? p.calls?.[0]?.arguments ?? {}, null, 2));
  };
  const decide = (body: unknown) => confirm("승인 결정을 운영 에이전트에 전달합니다.", () =>
    mgr<Approval>("POST", `/api/v1/approvals/${sel!.id}/decision?org=${route.org}`, body)
      .then(() => { setSel(null); list.reload(); }).catch((e: Error) => setErr(e.message)));
  return (
    <div className="grid2">
      <Panel title="승인함" sub="위험도 → 만료 임박순" actions={
        <select aria-label="상태" value={status} onChange={(e) => setStatus(e.target.value)}>
          {["pending", "approved", "edited", "rejected", "expired"].map((s) => <option key={s}>{s}</option>)}
        </select>}>
        <ErrorLine error={list.error} />
        {!list.data?.items.length ? <Empty>승인 요청이 없습니다.</Empty> : (
          <div className="table-wrap"><table><thead><tr><th>위험</th><th>행동</th><th>단위</th><th>요청자</th><th>만료</th></tr></thead>
            <tbody>{list.data.items.map((a) => <tr key={a.id} className={sel?.id === a.id ? "selected" : ""} onClick={() => pick(a)} style={{ cursor: "pointer" }}>
              <td><span className={`badge risk-${a.riskLevel}`}>{a.riskLevel}</span></td><td>{a.action}</td><td>{a.unitId}</td>
              <td title={a.requester}>{a.requester.slice(0, 24)}</td><td>{a.status === "pending" ? untilText(a.expiresAt) : a.status}</td></tr>)}</tbody></table></div>
        )}
      </Panel>
      <Panel title="상세" sub={sel ? sel.id : "요청을 고르세요"}>
        {!sel ? <Empty>왼쪽 목록에서 요청을 선택하면 payload·근거·트레이스를 봅니다.</Empty> : (
          <div>
            <p className="msg">{sel.action} · {sel.source} task <code>{sel.sourceRef.slice(0, 12)}</code> · 생성 {fmtTime(sel.createdAt)} · 해시 <code>{sel.payloadHash.slice(0, 12)}</code>
              {sel.traceId && <> · <a href={`/o/${route.org}/p/${route.proj}/e/${route.env}/traces/${sel.traceId}`}>트레이스</a></>}</p>
            <pre className="result" aria-label="payload">{JSON.stringify(JSON.parse(sel.payload || "{}"), null, 2)}</pre>
            {sel.status === "pending" ? <>
              <label htmlFor="ap-args">인자 수정 (수정 후 승인)</label>
              <textarea id="ap-args" rows={5} value={args} onChange={(e) => setArgs(e.target.value)} />
              <label htmlFor="ap-reason">사유</label>
              <input id="ap-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="반려 사유는 평가 골든셋 후보가 됩니다" />
              <div className="row">
                <button type="button" onClick={() => decide({ decision: "approve", reason })}>승인</button>
                <button type="button" onClick={() => { try { decide(editDecision(args, reason)); } catch (e) { setErr((e as Error).message); } }}>수정 후 승인</button>
                <button type="button" className="danger" onClick={() => decide({ decision: "reject", reason })}>반려</button>
              </div>
            </> : <p className="msg">결정 {sel.status} · {sel.decisionBy} · {sel.decisionReason} · 재개 {sel.callbackResult}</p>}
            <ErrorLine error={err} />
          </div>
        )}
      </Panel>
    </div>
  );
}
