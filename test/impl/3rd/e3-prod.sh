#!/usr/bin/env bash
# E3-15 prod 리허설 — .env.prod 의 CHANGE_ME 를 셸 env 로 덮어써(리허설 전용 값) 기동·시험 후 정리
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token
export ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 1; done
rc=0
test/impl/3rd/e3_smoke.py rehearsal-admin-token prod || rc=1
test/impl/3rd/e3-audit.sh prod 2>&1 | tail -1 | grep -q "audit chain ok" && echo "PASS E3-11 (prod)" || { echo "FAIL E3-11 (prod)"; rc=1; }
test/impl/3rd/e3_latency.py rehearsal-admin-token 200 || rc=1
make prod-stop >/dev/null 2>&1
exit $rc
