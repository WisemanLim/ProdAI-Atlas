package store

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/pressly/goose/v3"
)

// 00005: 방언별 SQL — 감사 로그 append-only 트리거(FR-PLT-IAM-08) + PostgreSQL 행 수준 보안(FR-PLT-TEN-05 shared, CMP-01).
// RLS 는 요청 경로가 `SET LOCAL ROLE atlas_rls` + `atlas.tenant` 를 설정한 트랜잭션에만 적용된다(배경 작업·마이그레이션은 제외).
func init() { goose.AddMigrationContext(upAppendOnlyRLS, downAppendOnlyRLS) }

// RLSTables 는 org_id 로 격리하는 테이블이다.
var RLSTables = []string{"agents", "agent_revisions", "deployments", "projects", "environments", "quotas", "api_keys", "audit_events"}

const rlsRole = "atlas_rls"

func upAppendOnlyRLS(ctx context.Context, tx *sql.Tx) error {
	var stmts []string
	if migrateEngine == "sqlite" {
		stmts = []string{
			`CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END`,
			`CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END`,
		}
	} else {
		stmts = []string{
			`CREATE OR REPLACE FUNCTION atlas_audit_immutable() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$ LANGUAGE plpgsql`,
			`CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION atlas_audit_immutable()`,
			`DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '` + rlsRole + `') THEN CREATE ROLE ` + rlsRole + ` NOLOGIN NOBYPASSRLS; END IF; END $$`,
			`GRANT ` + rlsRole + ` TO CURRENT_USER`,
			`GRANT USAGE ON SCHEMA public TO ` + rlsRole,
			`GRANT SELECT ON organizations TO ` + rlsRole,
		}
		for _, t := range RLSTables {
			priv := "SELECT, INSERT, UPDATE, DELETE"
			if t == "audit_events" {
				priv = "SELECT, INSERT"
			}
			stmts = append(stmts,
				fmt.Sprintf(`GRANT %s ON %s TO %s`, priv, t, rlsRole),
				fmt.Sprintf(`ALTER TABLE %s ENABLE ROW LEVEL SECURITY`, t),
				fmt.Sprintf(`ALTER TABLE %s FORCE ROW LEVEL SECURITY`, t),
				// 요청 역할(atlas_rls)일 때만 tenant 일치 행을 본다. '*' 는 플랫폼 관리자.
				fmt.Sprintf(`CREATE POLICY tenant_isolation ON %s USING (current_user <> '%s' OR org_id = current_setting('atlas.tenant', true) OR current_setting('atlas.tenant', true) = '*') WITH CHECK (current_user <> '%s' OR org_id = current_setting('atlas.tenant', true) OR current_setting('atlas.tenant', true) = '*')`, t, rlsRole, rlsRole))
		}
	}
	for _, s := range stmts {
		if _, err := tx.ExecContext(ctx, s); err != nil {
			return fmt.Errorf("%s: %w", s[:min(len(s), 60)], err)
		}
	}
	return nil
}

func downAppendOnlyRLS(ctx context.Context, tx *sql.Tx) error {
	var stmts []string
	if migrateEngine == "sqlite" {
		stmts = []string{`DROP TRIGGER audit_no_update`, `DROP TRIGGER audit_no_delete`}
	} else {
		stmts = []string{`DROP TRIGGER audit_no_change ON audit_events`, `DROP FUNCTION atlas_audit_immutable()`}
		for _, t := range RLSTables {
			stmts = append(stmts, fmt.Sprintf(`DROP POLICY tenant_isolation ON %s`, t), fmt.Sprintf(`ALTER TABLE %s DISABLE ROW LEVEL SECURITY`, t))
		}
	}
	for _, s := range stmts {
		if _, err := tx.ExecContext(ctx, s); err != nil {
			return err
		}
	}
	return nil
}
