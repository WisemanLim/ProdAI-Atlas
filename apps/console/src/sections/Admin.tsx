// 조직 관리 — 멤버·API 키·비밀·알림 규칙·쿼터 (PLT-IAM · PLT-TEN · PLT-SEC · X-NTF)
import { useState } from "react";
import { mgr } from "../lib/api";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, fmtTime, useData } from "../ui";

type Tab = "members" | "apikeys" | "secrets" | "notify" | "quotas";
const TABS: Record<Tab, string> = { members: "멤버", apikeys: "API 키", secrets: "비밀", notify: "알림", quotas: "쿼터" };

type Rule = { id: string; name: string; topic: string; severity: string; throttleS: number; enabled: boolean; channels: { type: string; url?: string; to?: string[] }[] };
type Delivery = { id: string; ruleId: string; topic: string; channel: string; target: string; status: string; attempts: number; error?: string; createdAt: string };
type Template = { id: string; name: string; topic: string; filter: unknown[]; severity: string };

export function Admin({ route, confirm }: { route: Route; confirm: (text: string, then: () => void) => void }) {
  const [tab, setTab] = useState<Tab>("notify");
  const o = route.org;
  return (
    <div>
      <div className="row" role="tablist" aria-label="조직 관리">
        {(Object.keys(TABS) as Tab[]).map((t) => <button type="button" role="tab" key={t} aria-selected={t === tab} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>{TABS[t]}</button>)}
      </div>
      {tab === "members" && <Members org={o} confirm={confirm} />}
      {tab === "apikeys" && <ApiKeys org={o} confirm={confirm} />}
      {tab === "secrets" && <Secrets org={o} confirm={confirm} />}
      {tab === "notify" && <Notify org={o} confirm={confirm} />}
      {tab === "quotas" && <Quotas org={o} confirm={confirm} />}
    </div>
  );
}

type P = { org: string; confirm: (text: string, then: () => void) => void };

function Members({ org, confirm }: P) {
  const list = useData(() => mgr<{ items: { user: string; relation: string; object: string }[] }>("GET", `/api/v1/orgs/${org}/members`), [org]);
  const [f, setF] = useState({ user: "", role: "developer", project: "" });
  const [err, setErr] = useState<string | null>(null);
  const send = (method: string, body: unknown) => confirm("멤버 권한을 바꿉니다.", () => mgr(method, `/api/v1/orgs/${org}/members`, body).then(() => list.reload()).catch((e: Error) => setErr(e.message)));
  return (
    <Panel title="멤버·역할" sub="OpenFGA 관계 튜플">
      <div className="row">
        <input aria-label="사용자" placeholder="Keycloak sub 또는 group:이름#member" value={f.user} onChange={(e) => setF({ ...f, user: e.target.value })} />
        <select aria-label="역할" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{["owner", "admin", "member", "viewer", "developer", "operator", "auditor"].map((r) => <option key={r}>{r}</option>)}</select>
        <input aria-label="프로젝트" placeholder="프로젝트(선택)" value={f.project} onChange={(e) => setF({ ...f, project: e.target.value })} />
        <button type="button" onClick={() => send("POST", f)}>추가</button>
      </div>
      <ErrorLine error={err ?? list.error} />
      {!list.data?.items.length ? <Empty>멤버가 없습니다(또는 인증 꺼짐).</Empty> : (
        <table><thead><tr><th>사용자</th><th>관계</th><th>대상</th><th /></tr></thead><tbody>{list.data.items.map((t) =>
          <tr key={t.user + t.relation + t.object}><td>{t.user}</td><td>{t.relation}</td><td>{t.object}</td>
            <td><button type="button" className="danger" onClick={() => send("DELETE", { user: t.user, role: t.relation, project: t.object.startsWith("project:") ? t.object.split("/")[1] : "" })}>제거</button></td></tr>)}</tbody></table>
      )}
    </Panel>
  );
}

function ApiKeys({ org, confirm }: P) {
  const list = useData(() => mgr<{ items: { id: string; name: string; prefix: string; scopes: string; expiresAt: string; lastUsedAt?: string; revokedAt?: string }[] }>("GET", `/api/v1/orgs/${org}/apikeys`), [org]);
  const [name, setName] = useState("ci");
  const [scopes, setScopes] = useState("read a2a:invoke");
  const [plain, setPlain] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Panel title="API 키" sub="평문은 발급 시 한 번만 표시">
      <div className="row">
        <input aria-label="이름" value={name} onChange={(e) => setName(e.target.value)} />
        <input aria-label="범위" value={scopes} onChange={(e) => setScopes(e.target.value)} />
        <button type="button" onClick={() => confirm("API 키를 발급합니다.", () => mgr<{ key: string }>("POST", `/api/v1/orgs/${org}/apikeys`, { name, scopes, expiresInDays: 90 })
          .then((r) => { setPlain(r.key); list.reload(); }).catch((e: Error) => setErr(e.message)))}>발급</button>
      </div>
      {plain && <p className="alert" role="status">새 키 (지금만 표시): <code>{plain}</code></p>}
      <ErrorLine error={err ?? list.error} />
      {!list.data?.items.length ? <Empty>키가 없습니다.</Empty> : (
        <table><thead><tr><th>이름</th><th>접두사</th><th>범위</th><th>만료</th><th>최근 사용</th><th /></tr></thead><tbody>{list.data.items.map((k) =>
          <tr key={k.id}><td>{k.name}</td><td><code>{k.prefix}</code></td><td>{k.scopes}</td><td>{fmtTime(k.expiresAt)}</td><td>{fmtTime(k.lastUsedAt)}</td>
            <td>{k.revokedAt ? "폐기됨" : <button type="button" className="danger" onClick={() => confirm("키를 폐기합니다.", () => mgr("DELETE", `/api/v1/orgs/${org}/apikeys/${k.id}`).then(() => list.reload()).catch((e: Error) => setErr(e.message)))}>폐기</button>}</td></tr>)}</tbody></table>
      )}
    </Panel>
  );
}

function Secrets({ org, confirm }: P) {
  const list = useData(() => mgr<{ items: { name: string; version: number; updatedAt: string }[]; backend: string }>("GET", `/api/v1/orgs/${org}/secrets`), [org]);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const write = (path: string, method: string) => confirm("비밀을 기록합니다(값은 다시 볼 수 없음).", () =>
    mgr(method, path, { value }).then(() => { setValue(""); list.reload(); }).catch((e: Error) => setErr(e.message)));
  return (
    <Panel title="비밀" sub={`쓰기 전용 · 백엔드 ${list.data?.backend ?? "…"}`}>
      <div className="row">
        <input aria-label="이름" placeholder="이름" value={name} onChange={(e) => setName(e.target.value)} />
        <input aria-label="값" type="password" placeholder="값" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" />
        <button type="button" disabled={!name || !value} onClick={() => write(`/api/v1/orgs/${org}/secrets/${name}`, "PUT")}>저장</button>
      </div>
      <ErrorLine error={err ?? list.error} />
      {!list.data?.items.length ? <Empty>비밀이 없습니다.</Empty> : (
        <table><thead><tr><th>이름</th><th>버전</th><th>갱신</th><th /></tr></thead><tbody>{list.data.items.map((s) =>
          <tr key={s.name}><td>{s.name}</td><td>v{s.version}</td><td>{fmtTime(s.updatedAt)}</td>
            <td><button type="button" disabled={!value} onClick={() => write(`/api/v1/orgs/${org}/secrets/${s.name}/rotate`, "POST")}>입력값으로 회전</button></td></tr>)}</tbody></table>
      )}
    </Panel>
  );
}

function Notify({ org, confirm }: P) {
  const rules = useData(() => mgr<{ items: Rule[] }>("GET", `/api/v1/orgs/${org}/notifications/rules`), [org]);
  const tpls = useData(() => mgr<{ items: Template[] }>("GET", "/api/v1/notifications/templates"), []);
  const dl = useData(() => mgr<{ items: Delivery[] }>("GET", `/api/v1/orgs/${org}/notifications/deliveries`), [org], 5000);
  const [tpl, setTpl] = useState("budget-80");
  const [kind, setKind] = useState<"webhook" | "email">("email");
  const [target, setTarget] = useState("ops@example.com");
  const [err, setErr] = useState<string | null>(null);
  const create = () => {
    const t = tpls.data?.items.find((x) => x.id === tpl);
    if (!t) return;
    const ch = kind === "email" ? { type: "email", to: target.split(",").map((s) => s.trim()) } : { type: "webhook", url: target, format: target.includes("slack") ? "slack" : "generic" };
    confirm("알림 규칙을 만듭니다.", () => mgr("POST", `/api/v1/orgs/${org}/notifications/rules`, { name: t.name, topic: t.topic, filter: t.filter, severity: t.severity, channels: [ch], throttleS: 300 })
      .then(() => { setErr(null); rules.reload(); }).catch((e: Error) => setErr(e.message)));
  };
  const act = (p: Promise<unknown>) => p.then(() => { rules.reload(); dl.reload(); }).catch((e: Error) => setErr(e.message));
  return (
    <div className="grid2">
      <Panel title="알림 규칙" sub="이벤트 → 조건 → webhook(HMAC)·email">
        <div className="row">
          <select aria-label="템플릿" value={tpl} onChange={(e) => setTpl(e.target.value)}>{tpls.data?.items.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
          <select aria-label="채널" value={kind} onChange={(e) => setKind(e.target.value as "webhook" | "email")}><option>email</option><option>webhook</option></select>
          <input aria-label="대상" value={target} onChange={(e) => setTarget(e.target.value)} />
          <button type="button" onClick={create}>추가</button>
        </div>
        <ErrorLine error={err ?? rules.error} />
        {!rules.data?.items.length ? <Empty>규칙이 없습니다.</Empty> : (
          <div className="table-wrap"><table><thead><tr><th>이름</th><th>topic</th><th>채널</th><th>상태</th><th /></tr></thead><tbody>{rules.data.items.map((r) =>
            <tr key={r.id}><td>{r.name}</td><td><code>{r.topic}</code></td><td>{r.channels.map((c) => c.type === "email" ? c.to?.join(",") : c.url).join(" · ")}</td>
              <td>{r.enabled ? "사용" : "중지"}</td>
              <td><button type="button" onClick={() => act(mgr("POST", `/api/v1/orgs/${org}/notifications/rules/${r.id}/test`))}>시험 발송</button>
                <button type="button" className="danger" onClick={() => confirm("규칙을 삭제합니다.", () => act(mgr("DELETE", `/api/v1/orgs/${org}/notifications/rules/${r.id}`)))}>삭제</button></td></tr>)}</tbody></table></div>
        )}
      </Panel>
      <Panel title="전달 기록" sub="최근 200건">
        {!dl.data?.items.length ? <Empty>전달 기록이 없습니다.</Empty> : (
          <div className="table-wrap"><table><thead><tr><th>시각</th><th>topic</th><th>채널</th><th>대상</th><th>상태</th></tr></thead><tbody>{dl.data.items.map((d) =>
            <tr key={d.id} title={d.error}><td>{fmtTime(d.createdAt)}</td><td>{d.topic}</td><td>{d.channel}</td><td>{d.target}</td>
              <td><span className={`badge b-${d.status.endsWith("sent") ? "allow" : "block"}`}>{d.status}</span> ×{d.attempts}</td></tr>)}</tbody></table></div>
        )}
      </Panel>
    </div>
  );
}

function Quotas({ org, confirm }: P) {
  const list = useData(() => mgr<{ items: { scope: string; scopeId: string; key: string; limit: number; used?: number }[] }>("GET", `/api/v1/orgs/${org}/quotas`), [org]);
  const [f, setF] = useState({ scope: "project", project: "default", key: "agents.max", limit: 20 });
  const [err, setErr] = useState<string | null>(null);
  return (
    <Panel title="쿼터" sub="하위 합 ≤ 상위 · 초과 시 plt.quota.exceeded">
      <div className="row">
        <select aria-label="범위" value={f.scope} onChange={(e) => setF({ ...f, scope: e.target.value })}><option>project</option><option>org</option></select>
        <input aria-label="프로젝트" value={f.project} onChange={(e) => setF({ ...f, project: e.target.value })} />
        <select aria-label="키" value={f.key} onChange={(e) => setF({ ...f, key: e.target.value })}>{["agents.max", "tasks.concurrent", "cpu.millicores", "storage.gb"].map((k) => <option key={k}>{k}</option>)}</select>
        <input aria-label="한도" type="number" value={f.limit} onChange={(e) => setF({ ...f, limit: Number(e.target.value) })} />
        <button type="button" onClick={() => confirm("쿼터를 바꿉니다.", () => mgr("PUT", `/api/v1/orgs/${org}/quotas`, f).then(() => { setErr(null); list.reload(); }).catch((e: Error) => setErr(e.message)))}>저장</button>
      </div>
      <ErrorLine error={err ?? list.error} />
      {!list.data?.items.length ? <Empty>쿼터가 없습니다.</Empty> : (
        <table><thead><tr><th>범위</th><th>대상</th><th>키</th><th>사용/한도</th></tr></thead><tbody>{list.data.items.map((q) =>
          <tr key={q.scope + q.scopeId + q.key}><td>{q.scope}</td><td>{q.scopeId}</td><td>{q.key}</td><td>{q.used ?? "—"} / {q.limit}</td></tr>)}</tbody></table>
      )}
    </Panel>
  );
}
