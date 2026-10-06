package store

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/pressly/goose/v3"
)

// 00011: 승인 정책·결정·위임 테이블에도 테넌트 RLS (00005 와 같은 정책, PostgreSQL 만).
func init() { goose.AddMigrationContext(upRLSM4b, downRLSM4b) }

var rlsM4bTables = []string{"approval_policies", "approval_decisions", "approval_delegations"}

func upRLSM4b(ctx context.Context, tx *sql.Tx) error {
	if migrateEngine == "sqlite" {
		return nil
	}
	for _, t := range rlsM4bTables {
		for _, s := range []string{
			fmt.Sprintf(`GRANT SELECT, INSERT, UPDATE, DELETE ON %s TO %s`, t, rlsRole),
			fmt.Sprintf(`ALTER TABLE %s ENABLE ROW LEVEL SECURITY`, t),
			fmt.Sprintf(`ALTER TABLE %s FORCE ROW LEVEL SECURITY`, t),
			fmt.Sprintf(`CREATE POLICY tenant_isolation ON %s USING (current_user <> '%s' OR org_id = current_setting('atlas.tenant', true) OR current_setting('atlas.tenant', true) = '*') WITH CHECK (current_user <> '%s' OR org_id = current_setting('atlas.tenant', true) OR current_setting('atlas.tenant', true) = '*')`, t, rlsRole, rlsRole),
		} {
			if _, err := tx.ExecContext(ctx, s); err != nil {
				return fmt.Errorf("%s: %w", s[:min(len(s), 60)], err)
			}
		}
	}
	return nil
}

func downRLSM4b(ctx context.Context, tx *sql.Tx) error {
	if migrateEngine == "sqlite" {
		return nil
	}
	for _, t := range rlsM4bTables {
		if _, err := tx.ExecContext(ctx, fmt.Sprintf(`DROP POLICY tenant_isolation ON %s`, t)); err != nil {
			return err
		}
	}
	return nil
}
