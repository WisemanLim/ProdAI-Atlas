import { describe, expect, it } from "vitest";
import { layout } from "./waterfall";

describe("waterfall layout", () => {
  it("normalizes offsets/durations and computes depth from the span tree", () => {
    const wf = [
      { span_id: "a", name: "invoke_agent", service: "al-run", offset_ms: 0, duration_ms: 50, status: "Unset" },
      { span_id: "b", name: "chat", service: "gw", offset_ms: 10, duration_ms: 5, status: "Unset" },
      { span_id: "c", name: "mcp.tools.call", service: "al-run", offset_ms: 20, duration_ms: 0, status: "Error" },
    ];
    const roots = [{ ...wf[0], children: [{ ...wf[1] }, { ...wf[2] }] }];
    const bars = layout(wf, roots);
    expect(bars.map((b) => [b.left, b.width, b.depth])).toEqual([[0, 100, 0], [20, 10, 1], [40, 0.5, 1]]);
  });
});
