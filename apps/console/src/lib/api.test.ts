import { describe, expect, it } from "vitest";
import { apiUrl, editDecision, fetchGatewayUp, fetchHealth, mgr, untilText } from "./api";

describe("api", () => {
  it("normalizes paths for same-origin proxy", () => {
    expect(apiUrl("/api/v1/agents")).toBe("/api/v1/agents");
    expect(apiUrl("health/manager")).toBe("/health/manager");
  });

  it("fetchHealth parses body and rejects non-2xx", async () => {
    const ok = async () => new Response(JSON.stringify({ status: "ok", env: "local" }), { status: 200 });
    await expect(fetchHealth(ok as typeof fetch)).resolves.toEqual({ status: "ok", env: "local" });
    const bad = async () => new Response("", { status: 503 });
    await expect(fetchHealth(bad as typeof fetch)).rejects.toThrow("HTTP 503");
  });

  it("fetchGatewayUp never throws", async () => {
    const down = async () => { throw new Error("ECONNREFUSED"); };
    await expect(fetchGatewayUp(down as typeof fetch)).resolves.toBe(false);
  });
});

import { svcUrl } from "./api";

describe("service urls", () => {
  it("builds same-origin /svc paths with encoded params", () => {
    expect(svcUrl("ob", "/v1/traces", { q: 'model = "x"', since: 3600, empty: "" })).toBe("/svc/ob/v1/traces?q=model+%3D+%22x%22&since=3600");
    expect(svcUrl("alrun", "runs")).toBe("/svc/alrun/runs");
  });
});

describe("M2 승인함 helpers", () => {
  it("editDecision 은 JSON 객체 인자만 받는다", () => {
    expect(editDecision('{"amount":100}', "축소")).toEqual({ decision: "edit", reason: "축소", payload: { arguments: { amount: 100 } } });
    expect(() => editDecision("1", "")).toThrow();
    expect(() => editDecision("{bad", "")).toThrow();
  });
  it("untilText 는 남은 시간을 사람이 읽게", () => {
    const now = Date.parse("2026-10-02T00:00:00Z");
    expect(untilText("2026-10-01T23:00:00Z", now)).toBe("만료");
    expect(untilText("2026-10-02T00:10:00Z", now)).toBe("10분");
    expect(untilText("2026-10-02T05:00:00Z", now)).toBe("5시간");
    expect(untilText("2026-10-05T00:00:00Z", now)).toBe("3일");
  });
  it("mgr 는 오류 메시지를 올린다", async () => {
    const f = (async () => new Response(JSON.stringify({ message: "권한 없음" }), { status: 403 })) as unknown as typeof fetch;
    await expect(mgr("POST", "/api/v1/approvals/x/decision", { decision: "approve" }, f)).rejects.toThrow("권한 없음");
  });
});

describe("MAT band helpers", () => {
  it("단계 → 띠 구간", async () => {
    const { phaseIndex, scoreClass, gateLabel } = await import("./maturity");
    expect([0, 1, 2, 3, 4].map(phaseIndex)).toEqual([0, 1, 1, 2, 2]);
    expect([90, 50, 10].map(scoreClass)).toEqual(["ok", "warn", "bad"]);
    expect(gateLabel("golden_set_50")).toBe("golden set 50");
  });
});
