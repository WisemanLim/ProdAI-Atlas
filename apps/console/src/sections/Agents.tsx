// 에이전트·배포 — Manager 카탈로그와 배포 상태
import { fetchAgents, fetchDeployments } from "../lib/api";
import type { ChipStatus } from "../lib/map";
import { Empty, ErrorLine, Panel, STATUS_LABEL, StatusDot, useData } from "../ui";

export function Agents() {
  const { data, error } = useData(async () => ({ agents: await fetchAgents(), deps: await fetchDeployments() }), [], 5000);
  return (
    <Panel title="에이전트 · 배포" sub="Manager 카탈로그(등록·리비전)와 배포 상태 — 헬스 폴링 5초">
      <ErrorLine error={error} />
      {!data ? <Empty>불러오는 중…</Empty> : data.agents.length === 0 ? <Empty>등록된 에이전트가 없습니다.</Empty> : (
        <div className="table-wrap"><table>
          <thead><tr><th>이름</th><th>단위</th><th>레이어</th><th>버전 · rev</th><th>환경</th><th>상태</th><th>엔드포인트</th></tr></thead>
          <tbody>{data.agents.map((a) => {
            const d = data.deps.find((x) => x.agentId === a.id && x.status !== "stopped");
            const st: ChipStatus = d?.status ?? "none";
            return <tr key={a.id}><td>{a.name}</td><td><code>{a.unitId}</code></td><td>{a.layer}</td><td>{a.version} · r{a.currentRev}</td>
              <td>{d?.env ?? "—"}</td><td><StatusDot status={st} /> {STATUS_LABEL[st]}{d?.message && <small className="msg"> {d.message}</small>}</td>
              <td><code>{d?.endpoint ?? "—"}</code></td></tr>;
          })}</tbody>
        </table></div>
      )}
    </Panel>
  );
}
