// 콘솔 SSO (FR-PLT-IAM-01): Keycloak Authorization Code + PKCE(S256). 토큰은 sessionStorage(탭 범위)에만 두고,
// 같은 출처의 /api·/svc 요청에 Authorization 과 조직·프로젝트 문맥 헤더를 붙인다. 인증 모드 off 면 아무것도 하지 않는다.
export type AuthConfig = { mode: "off" | "oidc"; issuer?: string; clientId?: string };
type Tokens = { access: string; refresh?: string; exp: number; user: string };

const KEY = "atlas.tokens";
const PKCE = "atlas.pkce";
let config: AuthConfig = { mode: "off" };

const b64url = (b: ArrayBuffer | Uint8Array) =>
  btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export function decodeJwt(token: string): Record<string, unknown> {
  try {
    return JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  } catch {
    return {};
  }
}

export async function pkcePair(): Promise<{ verifier: string; challenge: string }> {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  return { verifier, challenge };
}

function load(): Tokens | null {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) ?? "null");
  } catch {
    return null;
  }
}

function save(raw: { access_token: string; refresh_token?: string; expires_in: number }): Tokens {
  const claims = decodeJwt(raw.access_token);
  const t = { access: raw.access_token, refresh: raw.refresh_token, exp: Date.now() + raw.expires_in * 1000,
    user: String(claims.preferred_username ?? claims.sub ?? "user") };
  sessionStorage.setItem(KEY, JSON.stringify(t));
  return t;
}

const redirectUri = () => `${window.location.origin}/auth/callback`;

export async function login(): Promise<never> {
  const { verifier, challenge } = await pkcePair();
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  sessionStorage.setItem(PKCE, JSON.stringify({ verifier, state, back: window.location.pathname }));
  const q = new URLSearchParams({ client_id: config.clientId!, response_type: "code", scope: "openid", redirect_uri: redirectUri(),
    code_challenge: challenge, code_challenge_method: "S256", state });
  window.location.assign(`${config.issuer}/protocol/openid-connect/auth?${q}`);
  return new Promise<never>(() => {});
}

export function logout() {
  sessionStorage.removeItem(KEY);
  window.location.assign(`${config.issuer}/protocol/openid-connect/logout?${new URLSearchParams({ client_id: config.clientId!, post_logout_redirect_uri: window.location.origin + "/" })}`);
}

async function tokenRequest(body: Record<string, string>): Promise<Tokens> {
  const res = await fetch(`${config.issuer}/protocol/openid-connect/token`, { method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ client_id: config.clientId!, ...body }) });
  if (!res.ok) throw new Error(`token ${res.status}`);
  return save(await res.json());
}

async function handleCallback(): Promise<string> {
  const q = new URLSearchParams(window.location.search);
  const saved = JSON.parse(sessionStorage.getItem(PKCE) ?? "{}");
  sessionStorage.removeItem(PKCE);
  if (!q.get("code") || q.get("state") !== saved.state) throw new Error("로그인 상태 불일치 — 다시 시도하세요");
  await tokenRequest({ grant_type: "authorization_code", code: q.get("code")!, redirect_uri: redirectUri(), code_verifier: saved.verifier });
  return saved.back && saved.back !== "/auth/callback" ? saved.back : "/";
}

async function accessToken(): Promise<string | null> {
  let t = load();
  if (t && t.exp - Date.now() < 30_000 && t.refresh) {
    t = await tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh }).catch(() => null);
  }
  return t && t.exp > Date.now() ? t.access : null;
}

export const currentUser = () => load()?.user ?? null;
export const authMode = () => config.mode;

/** 앱 시작 전 호출: 설정 조회 → 콜백 처리 → 토큰 없으면 로그인 → fetch 래핑. */
export async function initAuth(): Promise<AuthConfig> {
  try {
    config = await (await fetch("/api/v1/auth/config")).json();
  } catch {
    config = { mode: "off" };
  }
  if (config.mode !== "oidc") return config;
  if (window.location.pathname === "/auth/callback") {
    window.history.replaceState(null, "", await handleCallback());
  }
  if (!(await accessToken())) await login();
  const orig = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    if (!url.startsWith("/api/") && !url.startsWith("/svc/")) return orig(input, init);
    const headers = new Headers(init.headers);
    const tok = await accessToken();
    if (!tok) return login();
    headers.set("Authorization", `Bearer ${tok}`);
    const p = window.location.pathname.split("/"); // /o/{org}/p/{proj}/…
    if (p[1] === "o" && p[2]) headers.set("X-Atlas-Org", p[2]);
    if (p[3] === "p" && p[4]) headers.set("X-Atlas-Project", p[4]);
    const res = await orig(input, { ...init, headers });
    if (res.status === 401) await login();
    return res;
  };
  return config;
}
