// Manager·Gateway API 클라이언트. 콘솔은 같은 출처 프록시로 호출한다(dev=Vite proxy, runtime=nginx):
//   /api/*           → manager /api/*
//   /health/manager  → manager /health
//   /gw/*            → gateway /*
export type Health = { status: string; env: string };

export type Unit = { id: string; name: string; layer: string; concept: string; skill: string };

export type Agent = {
  id: string; project: string; name: string; unitId: string; layer: string; concept: string;
  currentRev: number; version: string; updatedAt: string;
};

export type DeployStatus = "pending" | "starting" | "ready" | "degraded" | "failed" | "stopped";

export type Deployment = {
  id: string; agentId: string; agentName: string; unitId: string; rev: number; env: string;
  driver: string; endpoint: string; status: DeployStatus; message?: string; lastProbeAt?: string;
};

export function apiUrl(path: string): string {
  return path.startsWith("/") ? path : `/${path}`;
}

async function getJSON<T>(path: string, fetcher: typeof fetch): Promise<T> {
  const res = await fetcher(apiUrl(path));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

export const fetchHealth = (fetcher: typeof fetch = fetch) => getJSON<Health>("/health/manager", fetcher);
export const fetchUnits = (fetcher: typeof fetch = fetch) =>
  getJSON<{ items: Unit[] }>("/api/v1/catalog/units", fetcher).then((r) => r.items);
export const fetchAgents = (fetcher: typeof fetch = fetch) =>
  getJSON<{ items: Agent[] }>("/api/v1/agents", fetcher).then((r) => r.items);
export const fetchDeployments = (fetcher: typeof fetch = fetch) =>
  getJSON<{ items: Deployment[] }>("/api/v1/deployments", fetcher).then((r) => r.items);

export async function fetchGatewayUp(fetcher: typeof fetch = fetch): Promise<boolean> {
  try {
    const h = await getJSON<{ status: string }>("/gw/health", fetcher);
    return h.status === "ok";
  } catch {
    return false;
  }
}

export async function sendA2A(agentId: string, body: unknown, fetcher: typeof fetch = fetch): Promise<unknown> {
  const res = await fetcher(apiUrl(`/api/v1/agents/${agentId}/a2a`), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  if (!res.ok) throw new Error((json as { message?: string }).message ?? `HTTP ${res.status}`);
  return json;
}

// ---- v1: 플랫폼 서비스 (Manager /svc/{name} 프록시 경유) ----
export type SvcName = "gw" | "ob" | "ev" | "gr" | "mcpreg" | "alrun" | "bl" | "mat" | "mkt" | "rpt";

/** FastAPI 오류 본문 → 사람이 읽을 메시지 (detail 이 객체면 message·reasons·errors 를 펼친다). */
export function errorText(body: unknown, status: number): string {
  const b = (body ?? {}) as { message?: string; detail?: unknown };
  if (b.message) return b.message;
  const d = b.detail as { message?: string; verification?: { reasons?: string[] }; errors?: { path: string; message: string }[] } | string | undefined;
  if (typeof d === "string") return d;
  if (d && typeof d === "object") {
    const extra = d.verification?.reasons?.join(" · ") ?? d.errors?.map((e) => `${e.path}: ${e.message}`).join(" · ");
    return [d.message, extra].filter(Boolean).join(" — ") || JSON.stringify(d);
  }
  return `HTTP ${status}`;
}

export function svcUrl(name: SvcName, path: string, params?: Record<string, string | number | undefined>): string {
  const q = params ? Object.entries(params).filter(([, v]) => v !== undefined && v !== "") : [];
  const qs = q.length ? `?${new URLSearchParams(q.map(([k, v]) => [k, String(v)])).toString()}` : "";
  return `/svc/${name}${path.startsWith("/") ? path : `/${path}`}${qs}`;
}

export async function svcGet<T>(name: SvcName, path: string, params?: Record<string, string | number | undefined>, fetcher: typeof fetch = fetch): Promise<T> {
  const res = await fetcher(svcUrl(name, path, params));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(body, res.status));
  return body as T;
}

export async function svcPost<T>(name: SvcName, path: string, body: unknown, fetcher: typeof fetch = fetch, method = "POST"): Promise<T> {
  const res = await fetcher(svcUrl(name, path), { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(out, res.status));
  return out as T;
}

// ---- M2: Manager 관리 API (승인함·조직 관리·A2A 레지스트리) ----
export async function mgr<T>(method: string, path: string, body?: unknown, fetcher: typeof fetch = fetch): Promise<T> {
  const res = await fetcher(apiUrl(path), body === undefined ? { method } : { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const out = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((out as { message?: string }).message ?? `HTTP ${res.status}`);
  return out as T;
}

export type Approval = {
  id: string; org: string; project: string; source: string; sourceRef: string; agentId: string; unitId: string; action: string;
  riskLevel: "low" | "medium" | "high" | "critical"; payload: string; payloadHash: string; traceId: string; requester: string;
  status: string; decisionBy?: string; decisionReason?: string; callbackResult?: string; expiresAt: string; createdAt: string;
};

/** 수정 후 승인 본문: 편집한 JSON 이 올바르면 {decision: edit, payload: {arguments}} — 아니면 오류. */
export function editDecision(argsText: string, reason: string): { decision: "edit"; reason: string; payload: { arguments: unknown } } {
  const args = JSON.parse(argsText);
  if (args === null || typeof args !== "object") throw new Error("arguments 는 JSON 객체여야 합니다");
  return { decision: "edit", reason, payload: { arguments: args } };
}

/** 만료까지 남은 시간 (승인함 정렬 보조 표시). */
export function untilText(iso: string, now = Date.now()): string {
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  if (s <= 0) return "만료";
  if (s < 3600) return `${Math.ceil(s / 60)}분`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간`;
  return `${Math.floor(s / 86400)}일`;
}
