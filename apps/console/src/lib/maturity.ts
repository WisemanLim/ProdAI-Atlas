// MAT 진화 띠·로드맵 표시 보조 (FR-MAT-09)
export type Band = { stage: number; name: string; phase: "Surface" | "Production" | "Compound"; progress_pct: number;
  score_by_attr: Record<string, number>; unmet_gates: string[]; next: string | null };

export const PHASES = ["Surface", "Production", "Compound"] as const;

/** 단계(L0~L4) → 띠 위 구간 인덱스 (L0=Surface, L1~2=Production, L3~4=Compound). */
export function phaseIndex(stage: number): number {
  return stage <= 0 ? 0 : stage <= 2 ? 1 : 2;
}

/** 점수 → 등급 색 클래스. */
export function scoreClass(score: number): string {
  return score >= 70 ? "ok" : score >= 40 ? "warn" : "bad";
}

/** 게이트 id → 사람이 읽는 칩 라벨. */
export function gateLabel(id: string): string {
  return id.replace(/_/g, " ");
}
