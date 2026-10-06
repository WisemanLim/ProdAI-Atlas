// trace 워터폴 막대 배치 (FR-OB-TRACE-03) — 서버의 offset_ms·duration_ms 를 0~100% 로 정규화하고 들여쓰기 깊이를 계산한다.
export type WSpan = { span_id: string; name: string; service: string; offset_ms: number; duration_ms: number; status: string };
export type TreeNode = WSpan & { children?: TreeNode[] };
export type Bar = WSpan & { left: number; width: number; depth: number };

export function depthMap(roots: TreeNode[]): Map<string, number> {
  const out = new Map<string, number>();
  const walk = (n: TreeNode, d: number) => {
    out.set(n.span_id, d);
    (n.children ?? []).forEach((c) => walk(c, d + 1));
  };
  roots.forEach((r) => walk(r, 0));
  return out;
}

export function layout(waterfall: WSpan[], roots: TreeNode[]): Bar[] {
  const end = Math.max(1, ...waterfall.map((s) => s.offset_ms + s.duration_ms));
  const depth = depthMap(roots);
  return waterfall.map((s) => ({
    ...s,
    left: Math.round((s.offset_ms / end) * 10000) / 100,
    width: Math.max(0.5, Math.round((s.duration_ms / end) * 10000) / 100),
    depth: depth.get(s.span_id) ?? 0,
  }));
}
