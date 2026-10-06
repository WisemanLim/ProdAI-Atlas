import { describe, expect, it } from "vitest";
import type { Agent, Deployment, Unit } from "./api";
import { buildMap, coverage, unitStatus } from "./map";

const units: Unit[] = [
  { id: "AL-PLAN", name: "Planner", layer: "L1", concept: "01-agentic-loops", skill: "loop.plan" },
  { id: "AL-ACT", name: "Actor", layer: "L1", concept: "01-agentic-loops", skill: "loop.act" },
  { id: "A2A-CARD", name: "Agent Card Registry", layer: "L1", concept: "a2a", skill: "a2a.card" },
  { id: "GW-ROUTE", name: "Routing", layer: "L2", concept: "04-ai-gateway", skill: "gw.route" },
  { id: "OB-TRACE", name: "Traces", layer: "L3", concept: "08-observability", skill: "ob.trace" },
  { id: "MAT", name: "Maturity", layer: "band", concept: "maturity", skill: "mat.assess" },
];
const agent = (id: string, unitId: string): Agent =>
  ({ id, unitId, name: id, project: "default", layer: "L1", concept: "", currentRev: 1, version: "0.1.0", updatedAt: "" });
const dep = (agentId: string, status: Deployment["status"]): Deployment =>
  ({ id: agentId + status, agentId, agentName: agentId, unitId: "", rev: 1, env: "dev", driver: "compose", endpoint: "http://x", status });

describe("overview map", () => {
  it("keeps original card numbering and layer order (07 in L2, 06 in L3)", () => {
    const layers = buildMap(units, [], [], false);
    expect(layers.map((l) => l.id)).toEqual(["L1", "L2", "L3"]);
    expect(layers[0].cards.map((c) => c.no)).toEqual(["01", "02", "A2A", "03"]);
    expect(layers[1].cards.map((c) => c.no)).toEqual(["04", "05", "07"]);
    expect(layers[2].cards.map((c) => c.no)).toEqual(["06", "08", "09"]);
    expect(layers[0].cards[2].supplement).toBe(true);
  });

  it("places units on their concept card", () => {
    const [l1] = buildMap(units, [], [], false);
    expect(l1.cards[0].chips.map((c) => c.unitId)).toEqual(["AL-PLAN", "AL-ACT"]);
    expect(l1.cards[2].chips.map((c) => c.unitId)).toEqual(["A2A-CARD"]);
  });

  it("derives chip status from deployments (healthiest wins, stopped ignored)", () => {
    const agents = [agent("a1", "AL-PLAN")];
    expect(unitStatus("AL-PLAN", agents, [dep("a1", "failed"), dep("a1", "ready")], false)).toBe("ready");
    expect(unitStatus("AL-PLAN", agents, [dep("a1", "stopped")], false)).toBe("stopped");
    expect(unitStatus("AL-PLAN", agents, [], false)).toBe("stopped");
    expect(unitStatus("AL-ACT", agents, [], false)).toBe("none");
  });

  it("treats GW-* as platform service following gateway health", () => {
    expect(unitStatus("GW-ROUTE", [], [], true)).toBe("service");
    expect(unitStatus("GW-ROUTE", [], [], false)).toBe("none");
  });

  it("counts coverage of live units", () => {
    const layers = buildMap(units, [agent("a1", "AL-PLAN")], [dep("a1", "ready")], true);
    expect(coverage(layers)).toEqual({ live: 2, total: 5 }); // AL-PLAN + GW-ROUTE / MAT 는 띠라 카드 밖
  });
});
