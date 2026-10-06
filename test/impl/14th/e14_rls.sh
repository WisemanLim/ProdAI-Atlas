#!/usr/bin/env bash
# E14-16 PostgreSQL RLS (CMP-01 · FR-PLT-TEN-05) + 감사 append-only 트리거 — prod 스택의 db 컨테이너에서 직접 SQL 로 확인
set -uo pipefail
DB=${1:-prodai-atlas-prod-db-1}
q() { docker exec -i "$DB" psql -U atlas -d atlas -tAq -v ON_ERROR_STOP=0 "$@" 2>&1; }
pass=0; fail=0; chk() { if [ "$2" = "$3" ]; then echo "PASS $1 got=$2"; pass=$((pass+1)); else echo "FAIL $1 got=$2 want=$3"; fail=$((fail+1)); fi; }
total=$(q -c "SELECT count(*) FROM agents")
ghost=$(q -c "BEGIN; SET LOCAL ROLE atlas_rls; SELECT set_config('atlas.tenant','no-such-org',true); SELECT (SELECT count(*) FROM agents)+(SELECT count(*) FROM deployments)+(SELECT count(*) FROM audit_events)+(SELECT count(*) FROM api_keys)+(SELECT count(*) FROM projects); ROLLBACK;" | tail -1)
def=$(q -c "BEGIN; SET LOCAL ROLE atlas_rls; SELECT set_config('atlas.tenant','default',true); SELECT count(*) FROM agents WHERE org_id <> 'default'; ROLLBACK;" | tail -1)
own=$(q -c "BEGIN; SET LOCAL ROLE atlas_rls; SELECT set_config('atlas.tenant','default',true); SELECT count(*) > 0 FROM agents; ROLLBACK;" | tail -1)
cross=$(q -c "BEGIN; SET LOCAL ROLE atlas_rls; SELECT set_config('atlas.tenant','default',true); INSERT INTO projects (id, org_id, slug, name, created_at) VALUES ('rlsprobe00000000000000000x','other-org','x','x',now()); ROLLBACK;" | grep -c "row-level security")
noctx=$(q -c "BEGIN; SET LOCAL ROLE atlas_rls; SELECT count(*) FROM agents; ROLLBACK;" | tail -1)
upd=$(q -c "UPDATE audit_events SET action='x' WHERE seq=1" | grep -c "append-only")
del=$(q -c "DELETE FROM audit_events" | grep -c "append-only")
rls=$(q -c "SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity AND relname IN ('agents','agent_revisions','deployments','projects','environments','quotas','api_keys','audit_events')")
echo "agents total(system)=$total"
chk rls-enabled-tables "$rls" 8
chk unknown-tenant-sees-nothing "$ghost" 0
chk tenant-sees-only-own "$def" 0
chk tenant-sees-own-rows "$own" t
chk cross-tenant-insert-blocked "$cross" 1
chk no-tenant-context-sees-nothing "$noctx" 0
chk audit-update-blocked "$upd" 1
chk audit-delete-blocked "$del" 1
echo "rls: pass=$pass fail=$fail"; [ $fail -eq 0 ]
