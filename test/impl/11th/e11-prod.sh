#!/usr/bin/env bash
# E11-13 prod 리허설 — Manager 내장 콘솔(:8080) 대상 HTTP·UI E2E
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator
make local-stop >/dev/null 2>&1
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && curl -fsS -o /dev/null http://127.0.0.1:8007/readyz && break; sleep 1; done
make mcp-seed ENV=prod >/dev/null 2>&1; make eval-seed ENV=prod >/dev/null 2>&1
docker logs prodai-atlas-prod-manager-1 2>&1 | grep -m1 '"console"\|msg=console' 
rc=0
test/impl/11th/e11_http.py || rc=1
mkdir -p test/impl/11th/logs/prod
(cd apps/console && node e2e/console.e2e.mjs http://127.0.0.1:8080 ../../test/impl/11th/logs/prod) || rc=1
make prod-stop >/dev/null 2>&1
exit $rc
