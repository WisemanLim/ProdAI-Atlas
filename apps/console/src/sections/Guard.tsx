// 07 Guardrails — 판정 로그·정책·즉석 검사 (GR-IO)
import { useState } from "react";
import { svcGet, svcPost } from "../lib/api";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Verdict = { id: string; boundary: string; decision: string; categories: string[]; policy_version: string; created_at: string };
type CheckOut = { decision: string; reasons: string[]; categories: string[]; contents?: string[]; latency_ms: number };

export function Guard({ route }: { route: Route }) {
  const tenant = route.org;
  const verdicts = useData(() => svcGet<{ items: Verdict[] }>("gr", "/v1/guard/verdicts", { tenant, limit: 30 }), [tenant], 5000);
  const policy = useData(() => svcGet<{ source: string; bundle: { version: string; boundaries: Record<string, { checker: string; mode: string }[]> } }>("gr", `/v1/guard/policies/${tenant}`), [tenant]);
  const [text, setText] = useState("고객 900101-1234568 연락처 010-1234-5678 로 변경해줘");
  const [boundary, setBoundary] = useState("input");
  const [out, setOut] = useState<CheckOut | null>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="grid2">
      <Panel title="즉석 검사" sub="경계별 파이프라인 — PII·탈옥·유해성·형식·정책">
        <label htmlFor="gtext">내용</label>
        <textarea id="gtext" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
        <div className="row">
          <select aria-label="경계" value={boundary} onChange={(e) => setBoundary(e.target.value)}>
            {["input", "output", "tool_result", "retrieval"].map((b) => <option key={b}>{b}</option>)}
          </select>
          <button type="button" onClick={() => svcPost<CheckOut>("gr", "/v1/guard/check", { tenant, boundary, contents: [text] }).then((o) => { setOut(o); setErr(null); verdicts.reload(); }).catch((e: Error) => setErr(e.message))}>검사</button>
        </div>
        <ErrorLine error={err} />
        {out && <div className="subpanel"><p><span className={`badge b-${out.decision}`}>{out.decision}</span> {out.categories.join(", ")} <small>{out.latency_ms}ms</small></p>
          {out.reasons.length > 0 && <p className="msg">{out.reasons.join("; ")}</p>}{out.contents && <pre className="result">{out.contents[0]}</pre>}</div>}
        {policy.data && <p className="msg">정책 {policy.data.bundle.version} ({policy.data.source}) · 입력 경계: {policy.data.bundle.boundaries.input?.map((s) => `${s.checker}/${s.mode}`).join(" → ")}</p>}
      </Panel>
      <Panel title="판정 로그" sub="원문 미보관 — 해시·범주만">
        <ErrorLine error={verdicts.error} />
        {!verdicts.data?.items.length ? <Empty>판정이 없습니다.</Empty> : (
          <div className="table-wrap"><table><thead><tr><th>시각</th><th>경계</th><th>판정</th><th>범주</th><th>정책</th></tr></thead>
            <tbody>{verdicts.data.items.map((v) => <tr key={v.id}><td>{fmtTime(v.created_at)}</td><td>{v.boundary}</td>
              <td><span className={`badge b-${v.decision}`}>{v.decision}</span></td><td>{v.categories.join(", ")}</td><td>{v.policy_version}</td></tr>)}</tbody></table></div>
        )}
      </Panel>
    </div>
  );
}
