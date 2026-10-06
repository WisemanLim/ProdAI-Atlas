-- PLT-MGR 카탈로그 (FR-PLT-MGR-01·03·04). SQLite·PostgreSQL 공통 SQL 만 사용.
-- +goose Up
CREATE TABLE agents (
    id          VARCHAR(36)  PRIMARY KEY,
    project     VARCHAR(63)  NOT NULL,
    name        VARCHAR(63)  NOT NULL,
    unit_id     VARCHAR(32)  NOT NULL,
    layer       VARCHAR(8)   NOT NULL,
    concept     VARCHAR(64)  NOT NULL,
    current_rev INTEGER      NOT NULL,
    created_at  TIMESTAMP    NOT NULL,
    updated_at  TIMESTAMP    NOT NULL,
    deleted_at  TIMESTAMP    NULL
);
-- 소프트 삭제된 이름은 재등록 가능 (부분 인덱스: SQLite 3.8+ · PostgreSQL 공통)
CREATE UNIQUE INDEX ux_agents_project_name ON agents (project, name) WHERE deleted_at IS NULL;
CREATE INDEX ix_agents_unit ON agents (unit_id);

CREATE TABLE agent_revisions (
    agent_id   VARCHAR(36) NOT NULL REFERENCES agents (id),
    rev        INTEGER     NOT NULL,
    version    VARCHAR(64) NOT NULL,
    spec_hash  VARCHAR(64) NOT NULL,
    manifest   TEXT        NOT NULL,
    created_at TIMESTAMP   NOT NULL,
    PRIMARY KEY (agent_id, rev)
);

-- +goose Down
DROP TABLE agent_revisions;
DROP TABLE agents;
