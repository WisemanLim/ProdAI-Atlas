-- X-HITL 승인함 (FR-X-HITL-01·02·07) · X-NTF 알림 규칙·전달 기록 (FR-X-NTF-01·02·03). SQLite·PostgreSQL 공통.
-- +goose Up
CREATE TABLE approvals (
    id              VARCHAR(26)  PRIMARY KEY,
    org_id          VARCHAR(63)  NOT NULL,
    project         VARCHAR(63)  NOT NULL,
    source          VARCHAR(16)  NOT NULL,          -- loop | a2a | deploy | tool
    source_ref      VARCHAR(128) NOT NULL,
    agent_id        VARCHAR(36)  NOT NULL,
    unit_id         VARCHAR(32)  NOT NULL,
    action          VARCHAR(255) NOT NULL,
    risk_level      VARCHAR(16)  NOT NULL,          -- low | medium | high | critical
    payload         TEXT         NOT NULL,
    payload_hash    VARCHAR(64)  NOT NULL,
    trace_id        VARCHAR(64)  NOT NULL,
    requester       VARCHAR(255) NOT NULL,
    status          VARCHAR(16)  NOT NULL,          -- pending | approved | rejected | edited | expired
    decision_by     VARCHAR(255) NULL,
    decision_reason TEXT         NULL,
    edited_payload  TEXT         NULL,
    callback_result VARCHAR(64)  NULL,
    expires_at      TIMESTAMP    NOT NULL,
    created_at      TIMESTAMP    NOT NULL,
    decided_at      TIMESTAMP    NULL
);
CREATE INDEX ix_approvals_org_status ON approvals (org_id, status, expires_at);

CREATE TABLE notification_rules (
    id         VARCHAR(26)  PRIMARY KEY,
    org_id     VARCHAR(63)  NOT NULL,
    name       VARCHAR(128) NOT NULL,
    topic      VARCHAR(128) NOT NULL,               -- NATS subject 패턴 (gw.budget.* · plt.quota.exceeded …)
    filter     TEXT         NOT NULL,               -- JSON [{field, op, value}] — 모두 만족
    severity   VARCHAR(16)  NOT NULL,
    channels   TEXT         NOT NULL,               -- JSON [{type: webhook|email, …}]
    throttle_s INTEGER      NOT NULL,
    enabled    BOOLEAN      NOT NULL,
    created_at TIMESTAMP    NOT NULL
);

CREATE TABLE notifications (
    id         VARCHAR(26)  PRIMARY KEY,
    org_id     VARCHAR(63)  NOT NULL,
    rule_id    VARCHAR(26)  NOT NULL,
    topic      VARCHAR(128) NOT NULL,
    channel    VARCHAR(16)  NOT NULL,
    target     VARCHAR(512) NOT NULL,
    status     VARCHAR(16)  NOT NULL,               -- sent | failed | throttled
    attempts   INTEGER      NOT NULL,
    error      TEXT         NULL,
    created_at TIMESTAMP    NOT NULL
);
CREATE INDEX ix_notifications_org_ts ON notifications (org_id, created_at);

-- +goose Down
DROP TABLE notifications;
DROP TABLE notification_rules;
DROP TABLE approvals;
