-- (agent, env) 당 활성 배포 1개 보장 — 다중 워커 동시 등록 경쟁 방지 (impl 5th 결함 수정).
-- +goose Up
UPDATE deployments SET status = 'stopped'
 WHERE status <> 'stopped'
   AND EXISTS (SELECT 1 FROM deployments d2
                WHERE d2.agent_id = deployments.agent_id AND d2.env = deployments.env
                  AND d2.status <> 'stopped' AND d2.id < deployments.id);
CREATE UNIQUE INDEX ux_deployments_active ON deployments (agent_id, env) WHERE status <> 'stopped';

-- +goose Down
DROP INDEX ux_deployments_active;
