-- 엔터프라이즈 IAM (FR-PLT-IAM-03·05·07·09 · PLT-TEN-04 · X-WSP-08):
-- SCIM 사용자·그룹·그룹→역할 매핑 · 세션 정책·세션·폐기 · 예산 상속 · 요금제·월 사용량·임계 기록
-- +goose Up
CREATE TABLE scim_users (
    id           VARCHAR(36)  PRIMARY KEY,
    org_id       VARCHAR(64)  NOT NULL,
    external_id  VARCHAR(256) NULL,
    user_name    VARCHAR(256) NOT NULL,
    display_name VARCHAR(256) NULL,
    emails       TEXT         NULL,
    active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMP    NOT NULL,
    updated_at   TIMESTAMP    NOT NULL
);
CREATE UNIQUE INDEX ux_scim_users_name ON scim_users (org_id, user_name);
CREATE TABLE scim_groups (
    id           VARCHAR(36)  PRIMARY KEY,
    org_id       VARCHAR(64)  NOT NULL,
    external_id  VARCHAR(256) NULL,
    display_name VARCHAR(256) NOT NULL,
    members      TEXT         NOT NULL,
    created_at   TIMESTAMP    NOT NULL,
    updated_at   TIMESTAMP    NOT NULL
);
CREATE UNIQUE INDEX ux_scim_groups_name ON scim_groups (org_id, display_name);
CREATE TABLE scim_group_roles (
    org_id   VARCHAR(64)  NOT NULL,
    grp      VARCHAR(256) NOT NULL,
    role     VARCHAR(64)  NOT NULL,
    project  VARCHAR(64)  NOT NULL DEFAULT '',
    env      VARCHAR(64)  NOT NULL DEFAULT '',
    PRIMARY KEY (org_id, grp, role, project, env)
);
CREATE TABLE org_security (
    org_id       VARCHAR(64) PRIMARY KEY,
    mfa_admin    BOOLEAN     NOT NULL DEFAULT FALSE,
    acr_min      INTEGER     NOT NULL DEFAULT 2,
    idle_s       INTEGER     NOT NULL DEFAULT 1800,
    max_s        INTEGER     NOT NULL DEFAULT 43200,
    max_sessions INTEGER     NOT NULL DEFAULT 0,
    updated_at   TIMESTAMP   NOT NULL
);
CREATE TABLE auth_sessions (
    sid        VARCHAR(128) PRIMARY KEY,
    org_id     VARCHAR(64)  NOT NULL,
    sub        VARCHAR(256) NOT NULL,
    revoked    BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP    NOT NULL,
    last_seen  TIMESTAMP    NOT NULL
);
CREATE INDEX ix_auth_sessions_sub ON auth_sessions (sub);
CREATE TABLE session_revocations (
    sub        VARCHAR(256) PRIMARY KEY,
    org_id     VARCHAR(64)  NOT NULL,
    revoked_at TIMESTAMP    NOT NULL
);
CREATE TABLE budgets (
    id              VARCHAR(64)  PRIMARY KEY,
    org_id          VARCHAR(64)  NOT NULL,
    scope           VARCHAR(16)  NOT NULL,
    scope_id        VARCHAR(256) NOT NULL,
    period          VARCHAR(16)  NOT NULL,
    amount_usd      VARCHAR(32)  NOT NULL,
    soft_pct        INTEGER      NOT NULL,
    hard_action     VARCHAR(16)  NOT NULL,
    downgrade_alias VARCHAR(128) NULL,
    created_at      TIMESTAMP    NOT NULL
);
CREATE TABLE plans (
    id         VARCHAR(64) PRIMARY KEY,
    name       VARCHAR(128) NOT NULL,
    limits     TEXT        NOT NULL,
    updated_at TIMESTAMP   NOT NULL
);
CREATE TABLE usage_monthly (
    org_id VARCHAR(64) NOT NULL,
    month  VARCHAR(7)  NOT NULL,
    tokens BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, month)
);
CREATE TABLE quota_marks (
    org_id VARCHAR(64) NOT NULL,
    qkey   VARCHAR(64) NOT NULL,
    period VARCHAR(16) NOT NULL,
    level  INTEGER     NOT NULL,
    at     TIMESTAMP   NOT NULL,
    PRIMARY KEY (org_id, qkey, period, level)
);

-- +goose Down
DROP TABLE quota_marks;
DROP TABLE usage_monthly;
DROP TABLE plans;
DROP TABLE budgets;
DROP TABLE session_revocations;
DROP TABLE auth_sessions;
DROP TABLE org_security;
DROP TABLE scim_group_roles;
DROP TABLE scim_groups;
DROP TABLE scim_users;
