#!/usr/bin/env bash
# E4-07 prod 리허설 — 리허설 전용 키를 셸 env 로 주입
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token
export ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
make local-stop >/dev/null 2>&1
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 1; done
rc=0
test/impl/4th/e4_trace.py prod http://127.0.0.1:8000 http://127.0.0.1:8090 "$ATLAS_GATEWAY_AL_PLAN_KEY" || rc=1
make prod-stop >/dev/null 2>&1
exit $rc
