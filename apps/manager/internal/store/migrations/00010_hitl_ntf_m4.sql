-- 승인 정책·다단 승인·위임·SLA (FR-X-HITL-03·04·05 · GR-POL-06) · 알림 중복 억제·다이제스트·에스컬레이션·확인 (FR-X-NTF-04·05)
-- +goose Up
ALTER TABLE approvals ADD COLUMN approvers TEXT NULL;
ALTER TABLE approvals ADD COLUMN quorum INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN sla_seconds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE approvals ADD COLUMN escalate_to TEXT NULL;
ALTER TABLE approvals ADD COLUMN escalated_at TIMESTAMP NULL;
ALTER TABLE approvals ADD COLUMN policy_rule VARCHAR(128) NULL;
ALTER TABLE approvals ADD COLUMN preview TEXT NULL;
CREATE TABLE approval_policies (
    org_id     VARCHAR(64) PRIMARY KEY,
    policy     TEXT        NOT NULL,
    updated_at TIMESTAMP   NOT NULL
);
CREATE TABLE approval_decisions (
    id          VARCHAR(36)  PRIMARY KEY,
    org_id      VARCHAR(64)  NOT NULL,
    approval_id VARCHAR(36)  NOT NULL,
    decider     VARCHAR(256) NOT NULL,
    on_behalf   VARCHAR(256) NULL,
    decision    VARCHAR(16)  NOT NULL,
    reason      TEXT         NULL,
    signature   VARCHAR(64)  NOT NULL,
    created_at  TIMESTAMP    NOT NULL
);
CREATE UNIQUE INDEX ux_approval_decisions ON approval_decisions (approval_id, decider);
CREATE TABLE approval_delegations (
    org_id     VARCHAR(64)  NOT NULL,
    principal  VARCHAR(256) NOT NULL,
    delegate   VARCHAR(256) NOT NULL,
    until      TIMESTAMP    NOT NULL,
    created_at TIMESTAMP    NOT NULL,
    PRIMARY KEY (org_id, principal)
);
ALTER TABLE notification_rules ADD COLUMN dedup_window_s INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_rules ADD COLUMN dedup_keys TEXT NULL;
ALTER TABLE notification_rules ADD COLUMN digest VARCHAR(16) NULL;
ALTER TABLE notification_rules ADD COLUMN escalation TEXT NULL;
ALTER TABLE notifications ADD COLUMN fingerprint VARCHAR(64) NULL;
ALTER TABLE notifications ADD COLUMN payload TEXT NULL;
ALTER TABLE notifications ADD COLUMN acked_at TIMESTAMP NULL;
ALTER TABLE notifications ADD COLUMN acked_by VARCHAR(256) NULL;
ALTER TABLE notifications ADD COLUMN escalation_step INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_notifications_fp ON notifications (rule_id, fingerprint, created_at);

-- +goose Down
DROP INDEX ix_notifications_fp;
DROP TABLE approval_delegations;
DROP TABLE approval_decisions;
DROP TABLE approval_policies;
