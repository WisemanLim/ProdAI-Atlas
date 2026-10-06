#!/usr/bin/env bash
# E16 prod 리허설 — oidc 모드에서 A2A(서명 카드·레지스트리·위임·스트림·push·확장 카드). 리허설 시크릿만.
set -uo pipefail
cd "$(dirname "$0")/../../.."
L=test/impl/16th/logs
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator ATLAS_BATCH_POLL_BASE_SEC=1
export KEYCLOAK_ADMIN_PASSWORD=rehearsal-kc-admin ATLAS_KC_CI_SECRET=rehearsal-kc-ci OPENFGA_PRESHARED_KEY=rehearsal-fga
export ATLAS_MANAGER_BOOTSTRAP_KEY=atl_prod_rehearsalboot0000000000000_rehearsalonlysecret ATLAS_SECRET_REFRESH_S=5
export ATLAS_PUSH_ALLOW_HOSTS=host.docker.internal
make local-stop >/dev/null 2>&1
make prod-build >"$L/prod-build.txt" 2>&1 || { echo "FAIL prod-build"; tail -20 "$L/prod-build.txt"; exit 1; }
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 2; done
for _ in $(seq 1 90); do docker inspect prodai-atlas-prod-keycloak-1 --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy && break; sleep 3; done
ATLAS_TEST_TOKEN=$(test/impl/14th/gatekey.py "$KEYCLOAK_ADMIN_PASSWORD" | tail -1); export ATLAS_TEST_TOKEN
rc=0
(cd agents/runtime && uv run --frozen python ../../test/impl/16th/e16_a2a.py prod) || rc=1
make prod-stop >/dev/null 2>&1
echo "== rc=$rc"; exit $rc
