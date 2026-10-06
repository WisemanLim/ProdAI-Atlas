#!/usr/bin/env bash
# E9-11 prod 리허설 — 리허설 전용 토큰·키를 셸 env 로 주입
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse
make local-stop >/dev/null 2>&1
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
for _ in $(seq 1 90); do curl -fsS http://127.0.0.1:8006/readyz >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 1; done
rc=0
test/impl/9th/e9_ob.py prod "$ATLAS_GATEWAY_AL_PLAN_KEY" "$CLICKHOUSE_PASSWORD" || rc=1
make prod-stop >/dev/null 2>&1
exit $rc
