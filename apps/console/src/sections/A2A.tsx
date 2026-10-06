// A2A 레지스트리 — skill 검색(BM25)·서명 카드·가시성 (FR-A2A-CARD-01·03·06)
import { useState } from "react";
import { mgr } from "../lib/api";
import { Empty, ErrorLine, Panel, useData } from "../ui";

type Entry = { agentId: string; name: string; org: string; project: string; unitId: string; visibility: string; score: number; trust: string; skills: { id: string; name: string; description?: string }[] };

export function A2A() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const reg = useData(() => mgr<{ items: Entry[] }>("GET", `/api/v1/a2a/registry/agents?limit=50${query ? `&q=${encodeURIComponent(query)}` : ""}`), [query]);
  const [card, setCard] = useState<{ id: string; body: Record<string, unknown> } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="grid2">
      <Panel title="A2A 레지스트리" sub="볼 수 있는 ready 에이전트 — 질의 시 BM25 순">
        <form className="row" onSubmit={(e) => { e.preventDefault(); setQuery(q); }}>
          <input aria-label="검색" placeholder="skill·설명 검색 (예: 코드 리뷰)" value={q} onChange={(e) => setQ(e.target.value)} />
          <button type="submit">검색</button>
        </form>
        <ErrorLine error={reg.error ?? err} />
        {!reg.data?.items.length ? <Empty>에이전트가 없습니다.</Empty> : (
          <div className="table-wrap"><table><thead><tr><th>이름</th><th>skills</th><th>가시성</th>{query && <th>점수</th>}</tr></thead><tbody>{reg.data.items.map((a) =>
            <tr key={a.agentId} style={{ cursor: "pointer" }} onClick={() => mgr<Record<string, unknown>>("GET", `/api/v1/agents/${a.agentId}/card`).then((b) => setCard({ id: a.agentId, body: b })).catch((e: Error) => setErr(e.message))}>
              <td>{a.name}<br /><small>{a.unitId} · {a.org}/{a.project} · {a.trust}</small></td><td>{a.skills?.map((s) => s.id).join(", ")}</td><td>{a.visibility}</td>
              {query && <td>{a.score.toFixed(2)}</td>}</tr>)}</tbody></table></div>
        )}
      </Panel>
      <Panel title="서명 카드" sub={card ? card.id : "에이전트를 고르세요"}>
        {!card ? <Empty>카드는 Manager 가 JCS + JWS(ES256)로 서명합니다.</Empty> : <>
          <p className="msg">서명 {(card.body.signatures as unknown[] | undefined)?.length ?? 0}개 · kid {String(((card.body.signatures as { protected: string }[] | undefined)?.[0]?.protected ?? "").slice(0, 16))}…</p>
          <pre className="result">{JSON.stringify(card.body, null, 2)}</pre></>}
      </Panel>
    </div>
  );
}
