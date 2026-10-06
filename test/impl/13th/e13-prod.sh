#!/usr/bin/env bash
# E13 prod 리허설 — runtime 이미지·PostgreSQL 로 경제성 스모크 + Gateway·루프 회귀 (리허설 시크릿만)
set -uo pipefail
cd "$(dirname "$0")/../../.."
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator ATLAS_BATCH_POLL_BASE_SEC=1
make local-stop >/dev/null 2>&1
make prod-build >test/impl/13th/logs/prod-build.txt 2>&1 || { echo "FAIL prod-build"; exit 1; }
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 1; done
rc=0
test/impl/13th/e13_econ.py rehearsal-admin-token http://127.0.0.1:8090 prodai-atlas-prod_default || rc=1
test/impl/3rd/e3_smoke.py rehearsal-admin-token prod || rc=1
test/impl/6th/e6_loop.py prod || rc=1
docker inspect prodai-atlas-prod-gateway-1 --format 'gateway readonly={{.HostConfig.ReadonlyRootfs}} user={{.Config.User}}'
make prod-stop >/dev/null 2>&1
exit $rc
