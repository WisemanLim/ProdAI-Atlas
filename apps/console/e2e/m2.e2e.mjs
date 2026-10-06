// 콘솔 M2 화면 E2E — 승인함(루프 HITL → 수정 후 승인)·조직 관리(알림 규칙·시험 발송·전달 기록)·A2A 레지스트리.
// 사용: node e2e/m2.e2e.mjs <base_url> <screenshot_dir>   (인증 모드면 ATLAS_E2E_USER·ATLAS_E2E_PASS)
import { chromium } from "playwright-core";

const BASE = process.argv[2] ?? "http://127.0.0.1:8080";
const SHOTS = process.argv[3] ?? "/tmp";
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
let failed = 0;
const check = (id, ok, detail = "") => { failed ||= !ok; console.log(`${ok ? "PASS" : "FAIL"} ${id} ${detail}`); };
const nav = (page, name) => page.getByRole("navigation", { name: "섹션" }).getByRole("button", { name, exact: true }).click();

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
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

  // 루프 HITL → 승인함에 요청이 뜬다 → 수정 후 승인 → 런 완료
  await nav(page, "01 루프");
  await page.getByLabel("새 목표").fill("atlas_delete_all 로 정리");
  await page.getByRole("button", { name: "실행" }).click();
  await page.getByRole("group", { name: "사람 승인" }).waitFor({ timeout: 30000 });
  const runUrl = page.url();
  await nav(page, "승인함");
  await page.getByRole("heading", { name: "승인함" }).waitFor();
  const row = page.locator("tbody tr").filter({ hasText: "atlas_delete_all" }).first();
  await row.waitFor({ timeout: 20000 });
  await row.click();
  await page.getByLabel("payload").waitFor();
  await page.getByLabel("인자 수정 (수정 후 승인)").fill('{"scope": "tmp-only"}');
  await page.getByLabel("사유").fill("E2E 범위 축소");
  await page.screenshot({ path: `${SHOTS}/inbox.png`, fullPage: true });
  await page.getByRole("button", { name: "수정 후 승인", exact: true }).click();
  await page.getByText("왼쪽 목록에서 요청을 선택하면").waitFor({ timeout: 10000 });
  await page.goto(runUrl, { waitUntil: "networkidle" });
  await page.getByText("END_TURN").first().waitFor({ timeout: 30000 });
  check("E18-12a 승인함 수정 후 승인 → 같은 런 완료", true, runUrl);

  // 조직 관리 → 알림: 템플릿으로 규칙 추가 → 시험 발송 → 전달 기록
  await nav(page, "조직 관리");
  await page.getByRole("tab", { name: "알림" }).click();
  await page.getByLabel("템플릿").selectOption("budget-80");
  await page.getByLabel("채널").selectOption("email");
  await page.getByLabel("대상").fill("e2e-ops@example.test");
  await page.getByRole("button", { name: "추가", exact: true }).click();
  const rule = page.locator("tbody tr").filter({ hasText: "e2e-ops@example.test" }).first();
  await rule.waitFor({ timeout: 10000 });
  await rule.getByRole("button", { name: "시험 발송" }).click();
  await page.locator("tbody tr").filter({ hasText: "test:sent" }).first().waitFor({ timeout: 15000 });
  check("E18-12b 알림 규칙 추가 → 시험 발송 → 전달 기록", true);
  await page.screenshot({ path: `${SHOTS}/admin-notify.png`, fullPage: true });
  await rule.getByRole("button", { name: "삭제" }).click();
  for (const t of ["멤버", "API 키", "비밀", "쿼터"]) {
    await page.getByRole("tab", { name: t }).click();
    await page.locator(".panel h2").first().waitFor();
  }
  await page.getByRole("tab", { name: "비밀" }).click();
  await page.getByText(/백엔드 (file|bao)/).waitFor({ timeout: 10000 });
  check("E18-12c 조직 관리 탭(멤버·API 키·비밀·쿼터)", true);

  // A2A 레지스트리 검색 → 서명 카드
  await nav(page, "A2A");
  await page.getByLabel("검색").fill("코드 리뷰 diff");
  await page.getByRole("button", { name: "검색" }).click();
  const first = page.locator("tbody tr").first();
  await first.filter({ hasText: "ma-cr" }).waitFor({ timeout: 10000 }).catch(() => {});   // 질의 결과로 다시 그려질 때까지
  const top = await first.innerText();
  await first.click();
  await page.getByText(/서명 [1-9]개/).waitFor({ timeout: 10000 });
  check("E18-12d A2A 레지스트리 BM25 → 서명 카드", top.includes("ma-cr"), top.split("\n")[0]);
  await page.screenshot({ path: `${SHOTS}/a2a.png`, fullPage: true });
  check("E18-12e 브라우저 런타임 오류 없음", errors.length === 0, errors.join(" | ").slice(0, 300));
} catch (e) {
  check("E18-UI 예외", false, String(e).slice(0, 400));
  await page.screenshot({ path: `${SHOTS}/failure.png`, fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
