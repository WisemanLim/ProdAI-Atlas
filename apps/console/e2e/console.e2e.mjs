// 콘솔 v1 UI E2E — 호스트 Google Chrome 을 playwright-core 로 구동 (브라우저 다운로드 없음).
// 사용: node e2e/console.e2e.mjs <base_url> <screenshot_dir>
import { chromium } from "playwright-core";

const BASE = process.argv[2] ?? "http://127.0.0.1:5173";
const SHOTS = process.argv[3] ?? "/tmp";
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
let failed = 0;
const check = (id, ok, detail = "") => { failed ||= !ok; console.log(`${ok ? "PASS" : "FAIL"} ${id} ${detail}`); };

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
try {
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  // 인증 모드(oidc): Keycloak 로그인 → PKCE 콜백 → 콘솔 (FR-PLT-IAM-01). 계정은 env 로만 받는다.
  if (page.url().includes("/realms/atlas/")) {
    await page.locator("#username").fill(process.env.ATLAS_E2E_USER ?? "");
    await page.locator("#password").fill(process.env.ATLAS_E2E_PASS ?? "");
    await page.locator("#kc-login").click();
    await page.getByRole("heading", { name: /개요 맵/ }).waitFor({ timeout: 20000 });
    const who = await page.getByLabel("로그인 사용자").innerText();
    check("E14-14 SSO 로그인(PKCE) → 콘솔", who.includes(process.env.ATLAS_E2E_USER ?? "?"), page.url());
  }
  await page.getByRole("heading", { name: /개요 맵/ }).waitFor();
  check("E11-04a 개요 맵 + 기본 문맥 URL", page.url().endsWith("/o/default/p/default/e/dev/overview"), page.url());
  await page.screenshot({ path: `${SHOTS}/overview.png`, fullPage: true });

  const sections = [["에이전트·배포", "에이전트 · 배포"], ["01 루프", "루프 런"], ["02 MCP", "MCP 레지스트리"], ["05 경제성", "비용 시뮬레이터"], ["07 가드레일", "즉석 검사"],
                    ["08 관측", "trace 검색"], ["06 평가", "골든셋"]];
  for (const [nav, heading] of sections) {
    await page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name: nav }).click();
    await page.getByRole("heading", { name: heading }).first().waitFor({ timeout: 10000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${SHOTS}/${nav.replace(/[^a-z0-9가-힣]/gi, "_")}.png`, fullPage: true });
    check(`E11-04 ${nav}`, true, page.url());
  }

  // 카드 → 개념 화면
  await page.getByRole("button", { name: "개요 맵" }).click();
  await page.getByRole("button", { name: /07\s*Guardrails/ }).click();
  check("E11-05 카드 클릭 → 가드레일 화면", page.url().endsWith("/guard"), page.url());

  // 가드 즉석 검사
  await page.getByRole("button", { name: "검사" }).click();
  await page.locator(".subpanel .badge").first().waitFor();
  check("E11-06 가드 즉석 검사 transform", (await page.locator(".subpanel .badge").first().innerText()) === "transform");

  // 05 경제성 — 시뮬레이터 워터폴 (FR-EC-CTRL-06)
  await page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name: "05 경제성" }).click();
  await page.getByRole("button", { name: "계산" }).click();
  await page.getByRole("table", { name: "워터폴" }).waitFor({ timeout: 10000 });
  const levers = await page.getByRole("table", { name: "워터폴" }).locator("tbody tr").count();
  check("E13-12 경제성 시뮬레이터 워터폴", levers === 5, `levers=${levers}`);

  // 루프 실행 → 상세
  await page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name: "01 루프" }).click();
  await page.getByLabel("새 목표").fill('atlas_calc({"expr":"2+3"}) 로 계산');
  await page.getByRole("button", { name: "실행" }).click();
  await page.locator(".timeline .ev-exit").waitFor({ timeout: 30000 });
  check("E11-07 UI 에서 런 실행 → 이벤트 타임라인", /\/runs\/[0-9a-f-]{36}$/.test(page.url()), page.url());

  // HITL 승인
  await page.getByLabel("새 목표").fill("atlas_delete_all 로 정리");
  await page.getByRole("button", { name: "실행" }).click();
  await page.getByRole("group", { name: "사람 승인" }).waitFor({ timeout: 30000 });
  await page.getByRole("button", { name: "승인", exact: true }).click();
  await page.getByText("END_TURN").first().waitFor({ timeout: 30000 });
  check("E11-08 HITL 대기 → 승인 → 완료", true);

  // trace 검색 → 워터폴
  await page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name: "08 관측" }).click();
  await page.getByLabel("검색 DSL").fill('service = "atlas-agent-al-run"');
  await page.getByRole("button", { name: "검색" }).click();
  await page.locator(".list button.link").filter({ hasText: /\b([3-9]|\d{2,}) spans/ }).first().click({ timeout: 20000 });
  await page.locator(".wf-row").first().waitFor({ timeout: 15000 });
  const rows = await page.locator(".wf-row").count();
  check("E11-09 trace 선택 → 워터폴", rows >= 2, `rows=${rows}`);
  await page.screenshot({ path: `${SHOTS}/waterfall.png`, fullPage: true });

  // prod 전환 → 확인 모달 → 띠
  await page.getByLabel("환경").selectOption("prod");
  await page.getByRole("dialog").waitFor();
  await page.getByRole("button", { name: "계속" }).click();
  await page.getByText("운영(prod) 환경 — 변경 작업은 확인 후 실행됩니다").waitFor();
  check("E11-10 prod 전환 확인 모달 → 색 띠·URL", page.url().includes("/e/prod/"), page.url());
  await page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name: "01 루프" }).click();
  await page.getByRole("button", { name: "실행" }).click();
  const modal = await page.getByRole("dialog").isVisible();
  await page.getByRole("button", { name: "취소" }).click();
  check("E11-11 prod 변경 작업 전 확인", modal);
  await page.screenshot({ path: `${SHOTS}/prod.png`, fullPage: true });
  check("E11-12 브라우저 런타임 오류 없음", errors.length === 0, errors.join(" | ").slice(0, 300));
} catch (e) {
  check("E11-UI 예외", false, String(e).slice(0, 400));
  await page.screenshot({ path: `${SHOTS}/failure.png`, fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
