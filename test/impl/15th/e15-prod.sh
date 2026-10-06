#!/usr/bin/env bash
# E15 prod 리허설 — OpenBao(file 스토리지) · Manager 비밀 중개 · Gateway 공급자 키 회전 (업스트림 = Gateway 자신의 OpenAI 호환 /v1)
set -uo pipefail
cd "$(dirname "$0")/../../.."
H=test/impl/15th; L=$H/logs
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator ATLAS_BATCH_POLL_BASE_SEC=1
export KEYCLOAK_ADMIN_PASSWORD=rehearsal-kc-admin ATLAS_KC_CI_SECRET=rehearsal-kc-ci OPENFGA_PRESHARED_KEY=rehearsal-fga
export ATLAS_MANAGER_BOOTSTRAP_KEY=atl_prod_rehearsalboot0000000000000_rehearsalonlysecret ATLAS_SECRET_REFRESH_S=5
export OPENAI_BASE_URL=http://gateway:8080/v1 OPENAI_API_KEYS=$ATLAS_GATEWAY_AL_PLAN_KEY   # 키 회전 검증용 업스트림(자기 자신)
make local-stop >/dev/null 2>&1
make prod-build >"$L/prod-build.txt" 2>&1 || { echo "FAIL prod-build"; tail -20 "$L/prod-build.txt"; exit 1; }
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8005/readyz 2>/dev/null | grep -q '"guard-secret"' && break; sleep 2; done
for _ in $(seq 1 90); do docker inspect prodai-atlas-prod-keycloak-1 --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy && break; sleep 3; done
docker logs prodai-atlas-prod-openbao-init-1 2>&1 | tail -2
ATLAS_TEST_TOKEN=$(test/impl/14th/gatekey.py "$KEYCLOAK_ADMIN_PASSWORD" | tail -1); export ATLAS_TEST_TOKEN
# 회전 대상 새 키 = Gateway 가상키(업스트림이 자기 자신이므로 /v1/models 검증 통과)
NEWVK=$(curl -s -X POST http://127.0.0.1:8090/admin/keys -H "Authorization: Bearer $ATLAS_GATEWAY_ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"tenant":"rotation","models":["*"]}' | python3 -c "import json,sys;print(json.load(sys.stdin)['key'])")
rc=0
$H/e15_secrets.py prod "$ATLAS_GATEWAY_ADMIN_TOKEN" "$NEWVK" || rc=1
$H/e15_bao.sh || rc=1
docker inspect prodai-atlas-prod-openbao-1 --format 'openbao {{.State.Status}}'
make prod-stop >/dev/null 2>&1
echo "== rc=$rc"; exit $rc
