// 02 MCP — 사설 레지스트리(승인·스캔·고정)와 테넌트 허용 도구 (MCP-REG)
import { svcGet } from "../lib/api";
import type { Route } from "../lib/router";
import { Empty, ErrorLine, Panel, useData } from "../ui";

type Entry = { server: { name: string; version: string; description: string }; _meta: Record<string, { status: string; trust: string; protocol: string; scanScore: number }> };
type Effective = { servers: { name: string; version: string; trust: string; pins: Record<string, string>; protocol: string }[]; tools: { include: string[]; exclude: string[] }; note?: string };

export function Mcp({ route }: { route: Route }) {
  const reg = useData(() => svcGet<{ servers: Entry[] }>("mcpreg", "/v0.1/servers", { limit: 100 }), [], 10000);
  const eff = useData(() => svcGet<Effective>("mcpreg", `/v0.1/tenants/${route.org}/effective`), [route.org], 10000);
  return (
    <div className="grid2">
      <Panel title="MCP 레지스트리" sub="등록 = 설치 전 스캔(≥70 거부) · 승인 = 도구 정의 고정(rug pull 차단)">
        <ErrorLine error={reg.error} />
        {!reg.data?.servers.length ? <Empty>등록된 서버가 없습니다 — <code>make mcp-seed</code></Empty> : (
          <div className="table-wrap"><table><thead><tr><th>서버</th><th>버전</th><th>상태</th><th>신뢰</th><th>스캔</th><th>프로토콜</th></tr></thead>
            <tbody>{reg.data.servers.map((e) => { const m = Object.values(e._meta)[0]; return (
              <tr key={e.server.name + e.server.version}><td><code>{e.server.name}</code></td><td>{e.server.version}</td>
                <td><span className={`badge b-${m.status}`}>{m.status}</span></td><td>{m.trust}</td><td>{m.scanScore}</td><td>{m.protocol}</td></tr>); })}</tbody></table></div>
        )}
      </Panel>
      <Panel title={`허용 도구 — ${route.org}`} sub="승인됨 ∩ 허용목록 (없으면 기본 거부 · Shadow MCP 차단)">
        <ErrorLine error={eff.error} />
        {eff.data && (eff.data.servers.length === 0 ? <Empty>{eff.data.note ?? "허용된 서버 없음"}</Empty> : (
          <>
            <p className="msg">include {eff.data.tools.include.join(", ") || "—"} · exclude {eff.data.tools.exclude.join(", ") || "—"}</p>
            {eff.data.servers.map((s) => (
              <div key={s.name} className="subpanel"><b>{s.name}</b> <small>{s.version} · {s.trust} · {s.protocol}</small>
                <ul className="chips">{Object.keys(s.pins).map((t) => <li key={t} className="chip s-ready"><span className="chip-name">{t}</span><span className="chip-id">pinned</span></li>)}</ul></div>
            ))}
          </>
        ))}
      </Panel>
    </div>
  );
}
