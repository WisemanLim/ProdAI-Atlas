#!/usr/bin/env bash
# E7-11 prod 리허설 — 리허설 전용 토큰·키를 셸 env 로 주입
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token
export ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin
make local-stop >/dev/null 2>&1
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
for _ in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:8002/readyz 2>/dev/null && break; sleep 1; done
make mcp-seed ENV=prod 2>&1 | grep -vE "^docker"
rc=0
test/impl/7th/e7_mcp.py prod || rc=1
test/impl/7th/e7-rugpull.sh prod rehearsal-mcp-admin || rc=1
make prod-stop >/dev/null 2>&1
exit $rc
