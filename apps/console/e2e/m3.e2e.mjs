// 콘솔 M3 화면 E2E — 홈 진화 띠(MAT) → 성숙도 화면 · 09 Bitter Lesson(후보·alias·프롬프트·플래그) · 06 평가(judge·스코어카드).
// 사용: node e2e/m3.e2e.mjs <base_url> <screenshot_dir>   (인증 모드면 ATLAS_E2E_USER·ATLAS_E2E_PASS)
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
  const marker = page.getByLabel("현재 위치");
  await marker.waitFor({ timeout: 20000 });
  const mtext = await marker.innerText();
  await page.screenshot({ path: `${SHOTS}/home-band.png`, fullPage: true });
  check("E21-12a 홈 진화 띠 현재 위치 마커", /▲ L[0-4] /.test(mtext), mtext);
  await page.getByLabel("다음 단계 미달 조건").click();
  await page.getByRole("heading", { name: /^L[0-4] / }).waitFor({ timeout: 20000 });
  await page.getByRole("table", { name: "커버리지" }).waitFor();
  const rows = await page.getByRole("table", { name: "커버리지" }).locator("tbody tr").count();
  await page.screenshot({ path: `${SHOTS}/maturity.png`, fullPage: true });
  check("E21-12b 띠 → 성숙도(4속성·게이트·커버리지 10개념)", rows === 10 && page.url().endsWith("/mat"), `rows=${rows}`);

  await nav(page, "09 Bitter Lesson");
  await page.getByRole("heading", { name: "모델 후보" }).waitFor();
  await page.getByRole("table", { name: "보정 플래그" }).waitFor();
  await page.waitForTimeout(800);
  const flags = await page.getByRole("table", { name: "보정 플래그" }).locator("tr").count();
  await page.screenshot({ path: `${SHOTS}/bitter-lesson.png`, fullPage: true });
  check("E21-12c Bitter Lesson 후보·alias·프롬프트·플래그", flags >= 1, `flags=${flags}`);

  await nav(page, "06 평가");
  await page.getByRole("table", { name: "judge" }).waitFor({ timeout: 15000 });
  await page.getByRole("table", { name: "스코어카드" }).waitFor({ timeout: 15000 });
  const kappa = await page.getByRole("table", { name: "judge" }).locator("tbody tr").count();
  await page.screenshot({ path: `${SHOTS}/evals-judge.png`, fullPage: true });
  check("E21-12d 평가 judge κ·스코어카드", kappa >= 1, `judges=${kappa}`);
  check("E21-12e 브라우저 런타임 오류 없음", errors.length === 0, errors.join(" | ").slice(0, 300));
} catch (e) {
  check("E21-UI 예외", false, String(e).slice(0, 400));
  await page.screenshot({ path: `${SHOTS}/failure.png`, fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
