-- 배포 고도화 (FR-PLT-DEP-05·06·07·08·09·10 · PLT-MGR-07·09): 배포 슬롯(primary·canary·standby)·가중치·드리프트·복제 수,
-- 롤아웃(카나리·블루그린), 환경별 배포 정책, 관찰 토폴로지(A2A 호출 간선).
-- +goose Up
ALTER TABLE deployments ADD COLUMN slot VARCHAR(16) NOT NULL DEFAULT 'primary';
ALTER TABLE deployments ADD COLUMN weight INTEGER NOT NULL DEFAULT 100;
ALTER TABLE deployments ADD COLUMN drift TEXT NULL;
ALTER TABLE deployments ADD COLUMN replicas INTEGER NOT NULL DEFAULT 1;
ALTER TABLE deployments ADD COLUMN rollout_id VARCHAR(36) NULL;
ALTER TABLE deployments ADD COLUMN calls INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deployments ADD COLUMN errors INTEGER NOT NULL DEFAULT 0;
DROP INDEX ux_deployments_active;
CREATE UNIQUE INDEX ux_deployments_active_slot ON deployments (agent_id, env, slot) WHERE status <> 'stopped';
CREATE TABLE rollouts (
    id                  VARCHAR(36)  PRIMARY KEY,
    org_id              VARCHAR(64)  NOT NULL,
    agent_id            VARCHAR(36)  NOT NULL,
    env                 VARCHAR(32)  NOT NULL,
    strategy            VARCHAR(16)  NOT NULL,
    rev                 INTEGER      NOT NULL,
    from_deployment_id  VARCHAR(36)  NULL,
    to_deployment_id    VARCHAR(36)  NOT NULL,
    steps               TEXT         NOT NULL,
    step_index          INTEGER      NOT NULL DEFAULT 0,
    status              VARCHAR(16)  NOT NULL,
    gate                TEXT         NOT NULL,
    reason              TEXT         NULL,
    history             TEXT         NOT NULL,
    created_by          VARCHAR(256) NOT NULL,
    created_at          TIMESTAMP    NOT NULL,
    updated_at          TIMESTAMP    NOT NULL
);
CREATE INDEX ix_rollouts_agent ON rollouts (org_id, agent_id, env);
CREATE TABLE deploy_policies (
    org_id     VARCHAR(64)  NOT NULL,
    env        VARCHAR(32)  NOT NULL,
    policy     TEXT         NOT NULL,
    updated_at TIMESTAMP    NOT NULL,
    PRIMARY KEY (org_id, env)
);
CREATE TABLE topology_edges (
    org_id   VARCHAR(64)  NOT NULL,
    env      VARCHAR(32)  NOT NULL,
    caller   VARCHAR(256) NOT NULL,
    callee   VARCHAR(36)  NOT NULL,
    calls    INTEGER      NOT NULL DEFAULT 0,
    errors   INTEGER      NOT NULL DEFAULT 0,
    last_at  TIMESTAMP    NOT NULL,
    PRIMARY KEY (org_id, env, caller, callee)
);

-- +goose Down
DROP TABLE topology_edges;
DROP TABLE deploy_policies;
DROP TABLE rollouts;
DROP INDEX ux_deployments_active_slot;
CREATE UNIQUE INDEX ux_deployments_active ON deployments (agent_id, env) WHERE status <> 'stopped';
