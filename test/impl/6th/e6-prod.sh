#!/usr/bin/env bash
# E6-10 prod 리허설 — 리허설 전용 키를 셸 env 로 주입
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token
export ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
make local-stop >/dev/null 2>&1
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
rc=0
test/impl/6th/e6_loop.py prod || rc=1
test/impl/6th/e6-restart.sh prod || rc=1
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-prod -f docker-compose.yml -f docker-compose.prod.yml "${EXTRA[@]}" --env-file .env.prod)
echo "restarts: $("${DC[@]}" ps --format '{{.Service}}={{.Status}}' | grep agent-al)"
"${DC[@]}" logs agent-al-plan agent-al-run 2>&1 | grep -ciE "already exists|DuplicateTable|Traceback" | sed 's/^/migration-race errors: /'
make prod-stop >/dev/null 2>&1
exit $rc
