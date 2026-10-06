#!/usr/bin/env bash
# M0 완료 게이트 — 전체 회귀 + prod 회귀 (Epic 1~5 스모크 일괄)
set -uo pipefail
rc=0
echo "== make test-all"; make test-all >/tmp/m0-test-all.txt 2>&1 && echo "PASS test-all" || { echo "FAIL test-all"; rc=1; }
grep -E "^ok|passed|Tests " /tmp/m0-test-all.txt
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token
export ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
echo "== prod"; make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
make db-migrate ENV=prod 2>&1 | tail -1
make db-ping ENV=prod 2>&1 | tail -1
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 1; done
test/impl/1st/e1-smoke.sh || rc=1
test/impl/2nd/e2_smoke.py || rc=1
test/impl/3rd/e3_smoke.py rehearsal-admin-token prod || rc=1
test/impl/4th/e4_trace.py prod http://127.0.0.1:8000 http://127.0.0.1:8090 "$ATLAS_GATEWAY_AL_PLAN_KEY" || rc=1
test/impl/5th/e5_e2e.py prod || rc=1
make prod-stop >/dev/null 2>&1
echo "== gate rc=$rc"; exit $rc
