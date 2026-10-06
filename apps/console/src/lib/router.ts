// 전역 컨텍스트 라우터 (FR-PLT-CON-04): /o/{org}/p/{proj}/e/{env}/{section}[/{id}]
export const SECTIONS = ["overview", "agents", "runs", "mcp", "a2a", "econ", "guard", "traces", "evals", "bl", "mat", "mkt", "rpt", "inbox", "admin"] as const;
export type Section = (typeof SECTIONS)[number];
export const ENVS = ["dev", "staging", "prod"] as const;
export type Env = (typeof ENVS)[number];

export type Route = { org: string; proj: string; env: Env; section: Section; id?: string };

export const DEFAULT_ROUTE: Route = { org: "default", proj: "default", env: "dev", section: "overview" };

const SEG = /^[a-zA-Z0-9._-]{1,64}$/;

export function parseRoute(pathname: string): Route {
  const p = pathname.split("/").filter(Boolean);
  if (p[0] !== "o" || p[2] !== "p" || p[4] !== "e") return DEFAULT_ROUTE;
  const [org, proj, env, section, id] = [p[1], p[3], p[5], p[6], p[7]];
  if (![org, proj].every((s) => s && SEG.test(s))) return DEFAULT_ROUTE;
  return {
    org,
    proj,
    env: (ENVS as readonly string[]).includes(env) ? (env as Env) : "dev",
    section: (SECTIONS as readonly string[]).includes(section) ? (section as Section) : "overview",
    id: id && SEG.test(id) ? id : undefined,
  };
}

export function buildPath(r: Route): string {
  const base = `/o/${r.org}/p/${r.proj}/e/${r.env}/${r.section}`;
  return r.id ? `${base}/${r.id}` : base;
}

/** prod 환경에서는 환경 전환·변경 작업 전에 확인을 받는다 (FR-PLT-CON-04). */
export function needsConfirm(current: Env, next: Partial<Route> | null, mutating = false): boolean {
  return (next?.env === "prod" && current !== "prod") || (mutating && current === "prod");
}

/** 도식 카드 → 개념 화면 (화면설계서: 카드가 곧 메뉴의 입구). */
export const CARD_SECTION: Record<string, Section> = {
  "01-agentic-loops": "runs", "02-mcp": "mcp", a2a: "a2a", "03-multi-agent": "agents",
  "04-ai-gateway": "traces", "05-inference-economics": "econ", "07-guardrails": "guard",
  "06-evals": "evals", "08-observability": "traces", "09-bitter-lesson": "bl",
};
