#!/usr/bin/env bash
# E14 prod 리허설 — oidc 모드·PostgreSQL RLS·Keycloak(start)·OpenFGA(PostgreSQL) + 기존 스모크 회귀(인증 shim). 리허설 시크릿만.
set -uo pipefail
cd "$(dirname "$0")/../../.."
H=test/impl/14th; L=$H/logs; mkdir -p "$L/ui-prod"
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator ATLAS_BATCH_POLL_BASE_SEC=1
export KEYCLOAK_ADMIN_PASSWORD=rehearsal-kc-admin ATLAS_KC_CI_SECRET=rehearsal-kc-ci OPENFGA_PRESHARED_KEY=rehearsal-fga
export ATLAS_MANAGER_BOOTSTRAP_KEY=atl_prod_rehearsalboot0000000000000_rehearsalonlysecret
make local-stop >/dev/null 2>&1
make prod-build >"$L/prod-build.txt" 2>&1 || { echo "FAIL prod-build"; tail -20 "$L/prod-build.txt"; exit 1; }
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 2; done
for _ in $(seq 1 90); do docker inspect prodai-atlas-prod-keycloak-1 --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy && break; sleep 3; done
curl -s http://127.0.0.1:8080/api/v1/auth/config; echo
rc=0
$H/e14_iam.py prod "$KEYCLOAK_ADMIN_PASSWORD" "$ATLAS_KC_CI_SECRET" || rc=1
$H/e14_rls.sh || rc=1
$H/e14_console.py "$KEYCLOAK_ADMIN_PASSWORD" http://127.0.0.1:8080 "$L/ui-prod" || rc=1
$H/e14_cli.py "$KEYCLOAK_ADMIN_PASSWORD" || rc=1
# 기존 스모크 회귀 — 인증 이전에 작성된 스크립트는 shim 이 Manager 요청에 게이트 키를 붙인다
ATLAS_TEST_TOKEN=$($H/gatekey.py "$KEYCLOAK_ADMIN_PASSWORD" | tail -1); export ATLAS_TEST_TOKEN
export PYTHONPATH="$PWD/$H/authshim" PATH="$PWD/$H/authshim/bin:$PATH"
test/impl/1st/e1-smoke.sh || rc=1
test/impl/3rd/e3_smoke.py rehearsal-admin-token prod || rc=1
test/impl/5th/e5_e2e.py prod || rc=1
test/impl/6th/e6_loop.py prod || rc=1
test/impl/11th/e11_http.py || rc=1
test/impl/13th/e13_econ.py rehearsal-admin-token http://127.0.0.1:8090 prodai-atlas-prod_default || rc=1
for c in manager keycloak openfga; do docker inspect prodai-atlas-prod-$c-1 --format "$c {{.State.Status}} readonly={{.HostConfig.ReadonlyRootfs}}"; done
make prod-stop >/dev/null 2>&1
echo "== rc=$rc"; exit $rc
