// 콘솔 M3 2차 화면 E2E — S101 템플릿·마켓(카탈로그·검증 배지·파라미터 폼 원클릭 설치·설치본) · S102 사용량 리포트(비용·감사 체인·이상·SIEM).
// 사용: node e2e/m3b.e2e.mjs <base_url> <screenshot_dir>   (인증 모드면 ATLAS_E2E_USER·ATLAS_E2E_PASS)
import { chromium } from "playwright-core";

const BASE = process.argv[2] ?? "http://127.0.0.1:5173";
const SHOTS = process.argv[3] ?? "/tmp";
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
let failed = 0;
const check = (id, ok, detail = "") => { failed ||= !ok; console.log(`${ok ? "PASS" : "FAIL"} ${id} ${detail}`); };
const nav = (page, name) => page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name, exact: true }).click();

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 1100 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
try {
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  if (page.url().includes("/realms/atlas/")) {
    await page.locator("#username").fill(process.env.ATLAS_E2E_USER ?? "");
    await page.locator("#password").fill(process.env.ATLAS_E2E_PASS ?? "");
    await page.locator("#kc-login").click();
  }
  await page.getByRole("heading", { name: /개요 맵/ }).waitFor({ timeout: 20000 });
  await nav(page, "템플릿·마켓");
  const cat = page.getByRole("table", { name: "템플릿" });
  await cat.waitFor({ timeout: 20000 });
  const n = await cat.locator("tbody tr").count();
  await cat.locator("tbody tr", { hasText: "FAQ 상담 에이전트" }).click();
  const badges = page.getByLabel("공급망 검증");
  await badges.getByText("서명 valid").waitFor({ timeout: 15000 });
  const btext = await badges.innerText();
  check("E24-16a 카탈로그·서명/SLSA/SBOM 배지", n >= 4 && /SLSA valid/.test(btext) && /SBOM present/.test(btext), `rows=${n} ${btext.replace(/\s+/g, " ")}`);
  const name = `ui-faq-${Date.now() % 100000}`;
  await page.getByLabel(/에이전트 이름/).fill(name);
  await page.getByLabel(/외부 API 키/).fill("sk-ui-test");
  const pwType = await page.getByLabel(/외부 API 키/).getAttribute("type");
  await page.getByRole("button", { name: "원클릭 설치" }).click();
  await page.getByRole("status").filter({ hasText: "설치됨" }).waitFor({ timeout: 60000 }); // 게이트 부하(누적 설치·감사) 하에서 설치가 20초를 넘길 때가 있다
  await page.getByRole("table", { name: "설치본" }).waitFor({ timeout: 20000 });
  const inst = await page.getByRole("table", { name: "설치본" }).locator("tbody tr").count();
  await page.screenshot({ path: `${SHOTS}/market.png`, fullPage: true });
  check("E24-16b 파라미터 폼(비밀=password)·원클릭 설치·설치본 목록", pwType === "password" && inst >= 1, `type=${pwType} installs=${inst}`);

  await nav(page, "사용량 리포트");
  await page.getByRole("table", { name: "사용량" }).waitFor({ timeout: 20000 });
  const urows = await page.getByRole("table", { name: "사용량" }).locator("tbody tr").count();
  await page.getByRole("table", { name: "감사" }).waitFor({ timeout: 20000 });
  await page.getByText(/체인 정상 · WORM 일치/).waitFor({ timeout: 20000 });
  const arows = await page.getByRole("table", { name: "감사" }).locator("tbody tr").count();
  await page.getByLabel("통화").selectOption("KRW");
  await page.getByText(/합계 .* KRW/).waitFor({ timeout: 15000 });
  await page.screenshot({ path: `${SHOTS}/reports.png`, fullPage: true });
  check("E24-16c 비용(통화 전환)·감사 체인/WORM 검증", urows >= 1 && arows >= 1, `usage=${urows} audit=${arows}`);
  check("E24-16d 브라우저 런타임 오류 없음", errors.length === 0, errors.join(" | ").slice(0, 300));
} catch (e) {
  check("E24-UI 예외", false, String(e).slice(0, 400));
  await page.screenshot({ path: `${SHOTS}/failure.png`, fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
