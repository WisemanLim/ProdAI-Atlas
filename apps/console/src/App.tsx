// prodai-atlas console — 전역 컨텍스트(조직·프로젝트·환경) 셸 + 개념 화면 + M2(A2A·승인함·조직 관리) (FR-PLT-CON-01·04)
import { useCallback, useEffect, useState } from "react";
import { fetchGatewayUp, fetchHealth, type Health } from "./lib/api";
import { authMode, currentUser, logout } from "./lib/auth";
import { buildPath, ENVS, needsConfirm, parseRoute, SECTIONS, type Route, type Section } from "./lib/router";
import { A2A } from "./sections/A2A";
import { Admin } from "./sections/Admin";
import { Agents } from "./sections/Agents";
import { BitterLesson } from "./sections/BitterLesson";
import { Econ } from "./sections/Econ";
import { Evals } from "./sections/Evals";
import { Guard } from "./sections/Guard";
import { Inbox } from "./sections/Inbox";
import { Market } from "./sections/Market";
import { Maturity } from "./sections/Maturity";
import { Reports } from "./sections/Reports";
import { Mcp } from "./sections/Mcp";
import { Overview } from "./sections/Overview";
import { Runs } from "./sections/Runs";
import { Traces } from "./sections/Traces";
import { ConfirmModal, useData } from "./ui";

const LABEL: Record<Section, string> = {
  overview: "개요 맵", agents: "에이전트·배포", runs: "01 루프", mcp: "02 MCP", a2a: "A2A", econ: "05 경제성", guard: "07 가드레일", traces: "08 관측", evals: "06 평가",
  bl: "09 Bitter Lesson", mat: "성숙도", mkt: "템플릿·마켓", rpt: "사용량 리포트", inbox: "승인함", admin: "조직 관리",
};

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));
  const [pendingConfirm, setPendingConfirm] = useState<{ text: string; then: () => void } | null>(null);
  const health = useData<Health>(() => fetchHealth(), [], 10000);
  const gw = useData<boolean>(() => fetchGatewayUp(), [], 10000);
  // 조직·프로젝트 목록 (FR-PLT-TEN-02) — 인증 모드에서는 볼 수 있는 것만 온다
  const orgs = useData<string[]>(() => fetch("/api/v1/orgs").then((r) => (r.ok ? r.json() : { items: [] }))
    .then((b: { items: { slug: string }[] }) => b.items.map((o) => o.slug)), []);
  const projects = useData<string[]>(() => fetch(`/api/v1/orgs/${route.org}/projects`).then((r) => (r.ok ? r.json() : { items: [] }))
    .then((b: { items: { slug: string }[] }) => b.items.map((p) => p.slug)), [route.org]);
  const withCurrent = (list: string[] | null | undefined, cur: string) => Array.from(new Set([cur, ...(list ?? [])]));

  useEffect(() => {
    if (window.location.pathname !== buildPath(route)) window.history.replaceState(null, "", buildPath(route));
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [route]);

  const navigate = useCallback((next: Partial<Route>) => {
    const go = () => setRoute((r) => {
      const n = { ...r, ...next } as Route;
      window.history.pushState(null, "", buildPath(n));
      return n;
    });
    if (needsConfirm(route.env, next)) setPendingConfirm({ text: "운영 환경으로 전환합니다. 이후 변경 작업은 실제 운영에 영향을 줍니다.", then: go });
    else go();
  }, [route.env]);

  const confirmMutation = useCallback((text: string, then: () => void) => {
    if (needsConfirm(route.env, null, true)) setPendingConfirm({ text, then });
    else then();
  }, [route.env]);

  return (
    <div className={`shell env-${route.env}`}>
      {route.env === "prod" && <div className="prod-band" role="status">운영(prod) 환경 — 변경 작업은 확인 후 실행됩니다</div>}
      <header className="topbar">
        <div className="brand"><span className="logo">A</span> ProdAI Atlas</div>
        <div className="switcher" aria-label="작업 문맥">
          <label>조직 <select value={route.org} onChange={(e) => navigate({ org: e.target.value, proj: "default", id: undefined })}>
            {withCurrent(orgs.data, route.org).map((o) => <option key={o}>{o}</option>)}</select></label>
          <label>프로젝트 <select value={route.proj} onChange={(e) => navigate({ proj: e.target.value, id: undefined })}>
            {withCurrent(projects.data, route.proj).map((p) => <option key={p}>{p}</option>)}</select></label>
          <label>환경 <select value={route.env} onChange={(e) => navigate({ env: e.target.value as Route["env"], id: undefined })}>
            {ENVS.map((e) => <option key={e}>{e}</option>)}</select></label>
        </div>
        <div className="env">
          <span className={`pill ${health.data?.status === "ok" ? "ok" : "bad"}`}>Manager {health.data ? health.data.status : health.error ? "연결 실패" : "…"}</span>
          <span className={`pill ${gw.data ? "ok" : "bad"}`}>Gateway {gw.data ? "ok" : "down"}</span>
          {authMode() === "oidc" && <span className="user" aria-label="로그인 사용자">{currentUser()} <button type="button" onClick={logout}>로그아웃</button></span>}
        </div>
      </header>
      <nav className="nav" aria-label="섹션">
        {SECTIONS.map((s) => <button type="button" key={s} className={s === route.section ? "active" : ""} aria-current={s === route.section ? "page" : undefined}
          onClick={() => navigate({ section: s, id: undefined })}>{LABEL[s]}</button>)}
      </nav>
      <main className="content">
        <p className="crumb">{route.org} › {route.proj} › {route.env} › {LABEL[route.section]}{route.id ? ` › ${route.id.slice(0, 12)}` : ""}</p>
        {route.section === "overview" && <Overview go={(section) => navigate({ section, id: undefined })} />}
        {route.section === "agents" && <Agents />}
        {route.section === "runs" && <Runs route={route} open={(id) => navigate({ id })} confirm={confirmMutation} />}
        {route.section === "mcp" && <Mcp route={route} />}
        {route.section === "econ" && <Econ />}
        {route.section === "guard" && <Guard route={route} />}
        {route.section === "traces" && <Traces route={route} open={(id) => navigate({ id })} />}
        {route.section === "evals" && <Evals />}
        {route.section === "a2a" && <A2A />}
        {route.section === "bl" && <BitterLesson confirm={confirmMutation} />}
        {route.section === "mat" && <Maturity route={route} />}
        {route.section === "mkt" && <Market route={route} confirm={confirmMutation} />}
        {route.section === "rpt" && <Reports route={route} />}
        {route.section === "inbox" && <Inbox route={route} confirm={confirmMutation} />}
        {route.section === "admin" && <Admin route={route} confirm={confirmMutation} />}
      </main>
      {pendingConfirm && <ConfirmModal text={pendingConfirm.text} onCancel={() => setPendingConfirm(null)}
        onOk={() => { pendingConfirm.then(); setPendingConfirm(null); }} />}
    </div>
  );
}
