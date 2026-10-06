// 개요 맵(SCR-B03) 데이터 모델 — 원본 도식의 3 레이어 × 번호 카드(+A2A 보완)에 단위 에이전트 상태를 겹친다.
import type { Agent, Deployment, DeployStatus, Unit } from "./api";

export type ChipStatus = DeployStatus | "service" | "none";

export type Chip = { unitId: string; name: string; status: ChipStatus; agentId?: string };
export type Card = { no: string; concept: string; title: string; tagline: string; chips: Chip[]; supplement?: boolean };
export type Layer = { id: "L1" | "L2" | "L3"; title: string; tagline: string; cards: Card[] };

const LAYERS: { id: Layer["id"]; title: string; tagline: string; cards: Omit<Card, "chips">[] }[] = [
  {
    id: "L1", title: "AGENT RUNTIME", tagline: "Build intelligent agents that get work done.",
    cards: [
      { no: "01", concept: "01-agentic-loops", title: "Agentic Loops", tagline: "Plan → Act → Observe → Reflect → repeat." },
      { no: "02", concept: "02-mcp", title: "MCP", tagline: "Standard connection for tools, resources and systems." },
      { no: "A2A", concept: "a2a", title: "A2A", tagline: "도식 누락 보완 — Agent Card 발견·원격 위임", supplement: true },
      { no: "03", concept: "03-multi-agent", title: "Subagents & Multi-Agent", tagline: "Delegate subtasks to agents with their own context." },
    ],
  },
  {
    id: "L2", title: "CONTROL PLANE", tagline: "Operate, protect and optimize at scale.",
    cards: [
      { no: "04", concept: "04-ai-gateway", title: "AI Gateway", tagline: "Central control plane for AI traffic and policies." },
      { no: "05", concept: "05-inference-economics", title: "Inference Economics", tagline: "Tokens are a unit of cost." },
      { no: "07", concept: "07-guardrails", title: "Guardrails", tagline: "Protect users, brand and data." },
    ],
  },
  {
    id: "L3", title: "RELIABILITY LAYER", tagline: "Measure, observe and keep improving.",
    cards: [
      { no: "06", concept: "06-evals", title: "Evals", tagline: "Programmatic tests of quality." },
      { no: "08", concept: "08-observability", title: "Observability", tagline: "See everything. Debug anything." },
      { no: "09", concept: "09-bitter-lesson", title: "The Bitter Lesson", tagline: "General methods + compute win." },
    ],
  },
];

// 같은 단위에 배포가 여러 개면 가장 건강한 상태를 보인다.
const RANK: Record<ChipStatus, number> = { ready: 6, degraded: 5, starting: 4, pending: 3, failed: 2, stopped: 1, service: 0, none: 0 };

export function unitStatus(unitId: string, agents: Agent[], deployments: Deployment[], gatewayUp: boolean): Chip["status"] {
  if (unitId.startsWith("GW-")) return gatewayUp ? "service" : "none"; // Gateway 는 플랫폼 서비스로 구동
  const ids = new Set(agents.filter((a) => a.unitId === unitId).map((a) => a.id));
  if (ids.size === 0) return "none";
  const live = deployments.filter((d) => ids.has(d.agentId) && d.status !== "stopped");
  if (live.length === 0) return "stopped";
  return live.reduce<ChipStatus>((best, d) => (RANK[d.status] > RANK[best] ? d.status : best), "stopped");
}

export function buildMap(units: Unit[], agents: Agent[], deployments: Deployment[], gatewayUp: boolean): Layer[] {
  return LAYERS.map((l) => ({
    ...l,
    cards: l.cards.map((c) => ({
      ...c,
      chips: units
        .filter((u) => u.concept === c.concept)
        .map((u) => ({
          unitId: u.id,
          name: u.name,
          status: unitStatus(u.id, agents, deployments, gatewayUp),
          agentId: agents.find((a) => a.unitId === u.id)?.id,
        })),
    })),
  }));
}

export function coverage(layers: Layer[]): { live: number; total: number } {
  const chips = layers.flatMap((l) => l.cards.flatMap((c) => c.chips));
  return { live: chips.filter((c) => c.status === "ready" || c.status === "service").length, total: chips.length };
}
