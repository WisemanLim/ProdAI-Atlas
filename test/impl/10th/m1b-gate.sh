#!/usr/bin/env bash
# M1 2차 완료 게이트 — test-all + 레드팀 + prod(migrate·db-ping·mcp-seed) + Epic 1~10 스모크 일괄
set -uo pipefail
rc=0
echo "== make test-all"; make test-all >/tmp/m1b-test-all.txt 2>&1 && echo "PASS test-all" || { echo "FAIL test-all"; rc=1; }
grep -E "^ok|passed|Tests " /tmp/m1b-test-all.txt
echo "== guard-redteam"; make guard-redteam >/tmp/m1b-redteam.txt 2>&1 && echo "PASS redteam" || { echo "FAIL redteam"; rc=1; }
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator
make local-stop >/dev/null 2>&1
echo "== prod"; make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
make db-migrate ENV=prod 2>&1 | tail -1
make db-ping ENV=prod 2>&1 | tail -1
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && curl -fsS -o /dev/null http://127.0.0.1:8007/readyz && curl -fsS -o /dev/null http://127.0.0.1:8006/readyz && break; sleep 1; done
make mcp-seed ENV=prod 2>&1 | grep -E "approve|allowlist"
test/impl/1st/e1-smoke.sh || rc=1
test/impl/2nd/e2_smoke.py || rc=1
test/impl/3rd/e3_smoke.py rehearsal-admin-token prod || rc=1
test/impl/4th/e4_trace.py prod http://127.0.0.1:8000 http://127.0.0.1:8090 "$ATLAS_GATEWAY_AL_PLAN_KEY" || rc=1
test/impl/5th/e5_e2e.py prod || rc=1
test/impl/6th/e6_loop.py prod || rc=1
test/impl/7th/e7_mcp.py prod || rc=1
test/impl/7th/e7-rugpull.sh prod rehearsal-mcp-admin || rc=1
test/impl/8th/e8_guard.py prod rehearsal-admin-token rehearsal-guard-admin "$ATLAS_GATEWAY_AL_PLAN_KEY" || rc=1
test/impl/9th/e9_ob.py prod "$ATLAS_GATEWAY_AL_PLAN_KEY" "$CLICKHOUSE_PASSWORD" || rc=1
test/impl/10th/e10-ev.sh prod "$ATLAS_GATEWAY_AL_PLAN_KEY" rehearsal-ev-curator || rc=1
make prod-stop >/dev/null 2>&1
echo "== gate rc=$rc"; exit $rc
