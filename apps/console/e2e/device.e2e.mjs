// CLI 장치 코드 승인 (RFC 8628) — 브라우저에서 verification_uri_complete 로 로그인. 사용: node e2e/device.e2e.mjs <url> (계정은 env)
import { chromium } from "playwright-core";

const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage();
try {
  await page.goto(process.argv[2], { waitUntil: "networkidle" });
  await page.locator("#username").fill(process.env.ATLAS_E2E_USER ?? "");
  await page.locator("#password").fill(process.env.ATLAS_E2E_PASS ?? "");
  await page.locator("#kc-login").click();
  await page.waitForLoadState("networkidle");
  const yes = page.locator("#kc-login"); // 동의 화면이 있으면 승인
  if (await yes.count()) await yes.click().catch(() => {});
  await page.waitForTimeout(500);
  console.log("device approved:", (await page.title()) || page.url());
} finally {
  await browser.close();
}
