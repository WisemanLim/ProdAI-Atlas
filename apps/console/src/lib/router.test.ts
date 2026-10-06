import { describe, expect, it } from "vitest";
import { buildPath, CARD_SECTION, DEFAULT_ROUTE, needsConfirm, parseRoute } from "./router";

describe("context router", () => {
  it("round-trips org/project/env/section/id", () => {
    const r = { org: "acme", proj: "support", env: "prod" as const, section: "runs" as const, id: "run-1" };
    expect(parseRoute(buildPath(r))).toEqual(r);
  });

  it("falls back safely on unknown or malformed paths", () => {
    expect(parseRoute("/")).toEqual(DEFAULT_ROUTE);
    expect(parseRoute("/o/a/p/b/e/qa/nope")).toMatchObject({ env: "dev", section: "overview" });
    expect(parseRoute("/o/<script>/p/b/e/dev/runs")).toEqual(DEFAULT_ROUTE);
  });

  it("requires confirmation when entering prod or mutating in prod", () => {
    expect(needsConfirm("dev", { env: "prod" })).toBe(true);
    expect(needsConfirm("prod", { env: "prod" })).toBe(false);
    expect(needsConfirm("prod", null, true)).toBe(true);
    expect(needsConfirm("staging", null, true)).toBe(false);
  });
});

describe("05 경제성 섹션", () => {
  it("경제성 카드는 econ 화면으로, URL 왕복 보존", () => {
    expect(CARD_SECTION["05-inference-economics"]).toBe("econ");
    expect(parseRoute("/o/acme/p/web/e/dev/econ").section).toBe("econ");
  });
});
