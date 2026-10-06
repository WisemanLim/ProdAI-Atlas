-- PLT-TEN·PLT-IAM (FR-PLT-TEN-01·02·03, FR-PLT-IAM-06·08). SQLite·PostgreSQL 공통 SQL 만 사용 (방언별 트리거·RLS 는 00005 Go 마이그레이션).
-- +goose Up
CREATE TABLE organizations (
    id           VARCHAR(26)  PRIMARY KEY,
    slug         VARCHAR(63)  NOT NULL UNIQUE,
    name         VARCHAR(128) NOT NULL,
    plan         VARCHAR(32)  NOT NULL,
    status       VARCHAR(16)  NOT NULL,          -- active | suspended | deleting
    data_region  VARCHAR(8)   NOT NULL,          -- kr | us | eu
    isolation    VARCHAR(16)  NOT NULL,          -- shared(RLS) | namespace | dedicated
    delete_after TIMESTAMP    NULL,
    created_at   TIMESTAMP    NOT NULL,
    updated_at   TIMESTAMP    NOT NULL
);

CREATE TABLE projects (
    id         VARCHAR(26)  PRIMARY KEY,
    org_id     VARCHAR(63)  NOT NULL,
    slug       VARCHAR(63)  NOT NULL,
    name       VARCHAR(128) NOT NULL,
    created_at TIMESTAMP    NOT NULL
);
CREATE UNIQUE INDEX ux_projects_org_slug ON projects (org_id, slug);

CREATE TABLE environments (
    id              VARCHAR(26)  PRIMARY KEY,
    org_id          VARCHAR(63)  NOT NULL,
    project         VARCHAR(63)  NOT NULL,
    name            VARCHAR(32)  NOT NULL,
    target_ref      VARCHAR(256) NOT NULL,
    promotion_order INTEGER      NOT NULL,
    created_at      TIMESTAMP    NOT NULL
);
CREATE UNIQUE INDEX ux_envs_org_project_name ON environments (org_id, project, name);

CREATE TABLE quotas (
    org_id   VARCHAR(63)  NOT NULL,
    scope    VARCHAR(16)  NOT NULL,              -- org | project
    scope_id VARCHAR(127) NOT NULL,              -- org slug | org/project
    qkey     VARCHAR(64)  NOT NULL,              -- agents.max · tasks.concurrent · cpu.millicores · storage.gb
    qlimit   INTEGER      NOT NULL,
    PRIMARY KEY (scope, scope_id, qkey)
);

CREATE TABLE api_keys (
    id           VARCHAR(26)  PRIMARY KEY,
    org_id       VARCHAR(63)  NOT NULL,
    prefix       VARCHAR(64)  NOT NULL UNIQUE,   -- atl_{env}_{ulid}
    hash         VARCHAR(128) NOT NULL,          -- Argon2id(secret, salt)
    salt         VARCHAR(64)  NOT NULL,
    name         VARCHAR(128) NOT NULL,
    owner        VARCHAR(255) NOT NULL,
    scopes       VARCHAR(512) NOT NULL,
    expires_at   TIMESTAMP    NOT NULL,
    last_used_at TIMESTAMP    NULL,
    revoked_at   TIMESTAMP    NULL,
    created_at   TIMESTAMP    NOT NULL
);

-- 변경 불가 감사 로그 (조직별 해시 체인 — RLS 격리와 일치). UPDATE·DELETE 는 00005 트리거가 막는다.
CREATE TABLE audit_events (
    id          VARCHAR(26)  PRIMARY KEY,
    seq         BIGINT       NOT NULL,          -- 조직별 체인 순번
    ts          TIMESTAMP    NOT NULL,
    org_id      VARCHAR(63)  NOT NULL,
    actor_type  VARCHAR(16)  NOT NULL,
    actor_id    VARCHAR(255) NOT NULL,
    actor_ip    VARCHAR(64)  NOT NULL,
    action      VARCHAR(64)  NOT NULL,
    resource    VARCHAR(255) NOT NULL,
    before_hash VARCHAR(64)  NOT NULL,
    after_hash  VARCHAR(64)  NOT NULL,
    result      VARCHAR(16)  NOT NULL,
    detail      TEXT         NOT NULL,
    prev_hash   VARCHAR(64)  NOT NULL,
    hash        VARCHAR(64)  NOT NULL
);
CREATE UNIQUE INDEX ux_audit_org_seq ON audit_events (org_id, seq);
CREATE INDEX ix_audit_org_ts ON audit_events (org_id, ts);

ALTER TABLE agents ADD COLUMN org_id VARCHAR(63) NOT NULL DEFAULT 'default';
ALTER TABLE agent_revisions ADD COLUMN org_id VARCHAR(63) NOT NULL DEFAULT 'default';
ALTER TABLE deployments ADD COLUMN org_id VARCHAR(63) NOT NULL DEFAULT 'default';
DROP INDEX ux_agents_project_name;
CREATE UNIQUE INDEX ux_agents_org_project_name ON agents (org_id, project, name) WHERE deleted_at IS NULL;

INSERT INTO organizations (id, slug, name, plan, status, data_region, isolation, created_at, updated_at)
VALUES ('00000000000000000000000000', 'default', 'Default', 'self-hosted', 'active', 'kr', 'shared', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT INTO projects (id, org_id, slug, name, created_at) VALUES ('00000000000000000000000001', 'default', 'default', 'Default', CURRENT_TIMESTAMP);
INSERT INTO environments (id, org_id, project, name, target_ref, promotion_order, created_at) VALUES
    ('00000000000000000000000002', 'default', 'default', 'dev', 'compose', 1, CURRENT_TIMESTAMP),
    ('00000000000000000000000003', 'default', 'default', 'staging', 'compose', 2, CURRENT_TIMESTAMP),
    ('00000000000000000000000004', 'default', 'default', 'prod', 'compose', 3, CURRENT_TIMESTAMP);

-- +goose Down
DROP INDEX ux_agents_org_project_name;
CREATE UNIQUE INDEX ux_agents_project_name ON agents (project, name) WHERE deleted_at IS NULL;
DROP TABLE audit_events;
DROP TABLE api_keys;
DROP TABLE quotas;
DROP TABLE environments;
DROP TABLE projects;
DROP TABLE organizations;
