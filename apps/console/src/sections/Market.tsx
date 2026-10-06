// S101 템플릿·마켓 — 카탈로그(유형·레이어·단위 필터) · 서명/출처/SBOM 배지 · 파라미터 폼 원클릭 설치 · 설치본 업데이트·업그레이드 (FR-X-MKT-01~05)
import { useMemo, useState } from "react";
import { svcGet, svcPost } from "../lib/api";
import type { Route } from "../lib/router";
import { coerce, fieldsOf, type JSONSchema } from "../lib/schemaForm";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Card = { name: string; version: string; kind: string; title: string; description: string; unit_ids: string[]; layer: string; visibility: string;
  status: string; signed: boolean; installs: number; changelog: string; replaced_by: string | null };
type Detail = Card & { params: JSONSchema; versions: string[];
  verification: { ok: boolean; signature: string; provenance: string; sbom: string; reasons: string[]; vulnerabilities: { component: string; advisory: string }[] } };
type Install = { id: string; template: string; version: string; project: string; status: string; target: Record<string, unknown>; created_at: string };
type Updates = { available: boolean; latest: string; changelog: { version: string; changes: string }[] };

/** 설치 대상 한 줄 요약 — 에이전트 이름·배포 상태 · MCP 서버 · 산출물 */
function targetText(t: Record<string, unknown>): string {
  if (t.agent_name) return `에이전트 ${String(t.agent_name)} · 배포 ${String(t.deployment_status ?? "—")}${t.rev ? ` · rev ${String(t.rev)}` : ""}`;
  if (t.mcp_server) return `MCP ${String(t.mcp_server)} · ${String(t.registry_status ?? "")}`;
  return t.artifact ? `산출물 ${String(t.artifact)}` : JSON.stringify(t);
}

const KINDS = ["", "agent", "mcp-server", "guardrail-policy", "eval-dataset", "workflow", "dashboard"];

export function Market({ route, confirm }: { route: Route; confirm: (text: string, then: () => void) => void }) {
  const [kind, setKind] = useState("");
  const [q, setQ] = useState("");
  const ctx = { tenant: route.org, project: route.proj };
  const cat = useData(() => svcGet<{ items: Card[] }>("mkt", "/v1/templates", { ...ctx, kind, q }), [route.org, route.proj, kind, q]);
  const [sel, setSel] = useState<{ name: string; version: string } | null>(null);
  const detail = useData(() => (sel ? svcGet<Detail>("mkt", `/v1/templates/${sel.name}/versions/${sel.version}`, ctx) : Promise.resolve(null)), [sel?.name, sel?.version]);
  const form = useData(() => (sel ? svcGet<{ schema: JSONSchema; uiSchema: Record<string, { "ui:widget"?: string }> }>("mkt", `/v1/templates/${sel.name}/versions/${sel.version}/form`)
    : Promise.resolve(null)), [sel?.name, sel?.version]);
  const fields = useMemo(() => (form.data ? fieldsOf(form.data.schema, form.data.uiSchema) : []), [form.data]);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const installs = useData(async () => {
    const r = await svcGet<{ items: Install[] }>("mkt", "/v1/installs", ctx);
    const ups = await Promise.all(r.items.map((i) => svcGet<Updates>("mkt", `/v1/installs/${i.id}/updates`).catch(() => null)));
    return r.items.map((i, k) => ({ ...i, updates: ups[k] }));
  }, [route.org, route.proj], 15000);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const pick = (c: Card) => { setSel({ name: c.name, version: c.version }); setValues({}); setMsg(null); setErr(null); };
  const install = () => {
    const { params, errors } = coerce(fields, { ...Object.fromEntries(fields.map((f) => [f.name, f.initial])), ...values });
    if (errors.length) { setErr(errors.join(" · ")); return; }
    confirm(`${sel!.name}@${sel!.version} 를 ${route.proj} 에 설치합니다.`, () =>
      svcPost<{ id: string; target: Record<string, unknown> }>("mkt", "/v1/installs", { template: sel!.name, version: sel!.version, ...ctx, params })
        .then((r) => { setMsg(`설치됨 ${r.id.slice(0, 8)} → ${JSON.stringify(r.target)}`); setErr(null); installs.reload(); cat.reload(); })
        .catch((e: Error) => setErr(e.message)));
  };
  const upgrade = (i: Install) => confirm(`${i.template} 를 업그레이드합니다 (회귀 평가 통과 필요).`, () =>
    svcPost("mkt", `/v1/installs/${i.id}/upgrade`, {}).then(() => installs.reload()).catch((e: Error) => setErr(e.message)));
  const d = detail.data;
  return (
    <>
      <div className="grid2">
        <Panel title="템플릿 카탈로그" sub="에이전트·MCP 서버·가드레일·평가셋 — 서명된 템플릿만 설치" actions={<>
          <select aria-label="유형" value={kind} onChange={(e) => setKind(e.target.value)}>{KINDS.map((k) => <option key={k} value={k}>{k || "전체 유형"}</option>)}</select>
          <input aria-label="검색" placeholder="검색" value={q} onChange={(e) => setQ(e.target.value)} /></>}>
          <ErrorLine error={cat.error} />
          {!cat.data?.items.length ? <Empty>템플릿 없음</Empty> : (
            <div className="table-wrap"><table aria-label="템플릿"><thead><tr><th>이름</th><th>유형</th><th>버전</th><th>단위</th><th>서명</th><th>설치</th></tr></thead>
              <tbody>{cat.data.items.map((c) => (
                <tr key={c.name} className={sel?.name === c.name ? "selected" : ""} onClick={() => pick(c)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && pick(c)}>
                  <td><b>{c.title}</b><br /><small>{c.name}</small>{c.status === "deprecated" && <span className="chip-gate">폐기 → {c.replaced_by}</span>}</td>
                  <td>{c.kind}</td><td>{c.version}</td><td>{c.unit_ids.join(" · ")}</td><td>{c.signed ? "✓" : "—"}</td><td>{c.installs}</td></tr>))}</tbody></table></div>)}
        </Panel>
        <Panel title={d ? `${d.title} ${d.version}` : "설치"} sub={d ? `${d.kind} · ${d.visibility} · ${d.layer}` : "왼쪽에서 템플릿 선택"}>
          <ErrorLine error={detail.error ?? err} />
          {msg && <p className="msg" role="status">{msg}</p>}
          {!d ? <Empty>선택된 템플릿 없음</Empty> : <>
            <p>{d.description}</p>
            <div className="meta" aria-label="공급망 검증">
              <span className={`pill ${d.verification.signature === "valid" ? "ok" : "bad"}`}>서명 {d.verification.signature}</span>
              <span className={`pill ${d.verification.provenance === "valid" ? "ok" : "bad"}`}>SLSA {d.verification.provenance}</span>
              <span className={`pill ${d.verification.sbom === "present" && !d.verification.vulnerabilities.length ? "ok" : "bad"}`}>SBOM {d.verification.sbom}</span>
            </div>
            {!d.verification.ok && <p className="alert">{d.verification.reasons.join(" · ")}</p>}
            <p><small>버전 {d.versions.join(", ")} · 변경: {d.changelog}</small></p>
            <form className="form-grid" onSubmit={(e) => { e.preventDefault(); install(); }}>
              {fields.map((f) => (
                <label key={f.name}>{f.label}{f.required && " *"}
                  {f.kind === "select" ? (
                    <select value={String(values[f.name] ?? f.initial)} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                      {f.options.map((o) => <option key={o}>{o}</option>)}</select>
                  ) : f.kind === "checkbox" ? (
                    <input type="checkbox" checked={Boolean(values[f.name] ?? f.initial)} onChange={(e) => setValues({ ...values, [f.name]: e.target.checked })} />
                  ) : (
                    <input type={f.kind === "password" ? "password" : f.kind === "number" ? "number" : "text"} autoComplete="off" value={String(values[f.name] ?? f.initial)}
                      onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />)}
                </label>))}
              <button type="submit" disabled={!d.verification.ok || d.status !== "published"}>원클릭 설치</button>
            </form>
          </>}
        </Panel>
      </div>
      <Panel title="설치된 템플릿" sub="새 버전 알림 · 업그레이드 전 회귀 평가 (mkt.upgrade.require_eval_pass)">
        <ErrorLine error={installs.error} />
        {!installs.data?.length ? <Empty>이 프로젝트에 설치된 템플릿 없음</Empty> : (
          <div className="table-wrap"><table aria-label="설치본"><thead><tr><th>템플릿</th><th>버전</th><th>대상</th><th>설치</th><th>업데이트</th></tr></thead>
            <tbody>{installs.data.map((i) => (
              <tr key={i.id}><td>{i.template}</td><td>{i.version}</td><td><small title={JSON.stringify(i.target)}>{targetText(i.target)}</small></td>
                <td>{fmtTime(i.created_at)}</td>
                <td>{i.updates?.available ? <><span className="chip-gate">{i.updates.latest} 가능</span> <small>{i.updates.changelog.map((c) => c.changes).join(" / ")}</small>{" "}
                  <button type="button" onClick={() => upgrade(i)}>업그레이드</button></> : "최신"}</td></tr>))}</tbody></table></div>)}
      </Panel>
    </>
  );
}
