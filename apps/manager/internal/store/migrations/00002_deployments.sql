-- PLT-DEP 배포 레코드 (FR-PLT-MGR-02·05·10). 상태: pending → starting → ready ⇄ degraded → failed · stopped
-- +goose Up
CREATE TABLE deployments (
    id            VARCHAR(36)  PRIMARY KEY,
    agent_id      VARCHAR(36)  NOT NULL REFERENCES agents (id),
    rev           INTEGER      NOT NULL,
    env           VARCHAR(32)  NOT NULL,
    driver        VARCHAR(16)  NOT NULL,
    endpoint      VARCHAR(512) NOT NULL,
    status        VARCHAR(16)  NOT NULL,
    message       TEXT         NULL,
    card          TEXT         NULL,
    fail_count    INTEGER      NOT NULL DEFAULT 0,
    last_probe_at TIMESTAMP    NULL,
    created_at    TIMESTAMP    NOT NULL,
    updated_at    TIMESTAMP    NOT NULL
);
CREATE INDEX ix_deployments_agent_env ON deployments (agent_id, env);
CREATE INDEX ix_deployments_status ON deployments (status);

-- +goose Down
DROP TABLE deployments;
