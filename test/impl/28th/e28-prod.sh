#!/usr/bin/env bash
# E28 prod 리허설 — oidc 스택에서 SCIM(그룹 → 역할·비활성화)·ABAC 조건·워크로드 신원·Token Exchange·MFA·세션 정책·예산 상속·요금제 할당량. 리허설 시크릿만.
set -uo pipefail
cd "$(dirname "$0")/../../.."
L=test/impl/28th/logs
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator ATLAS_BATCH_POLL_BASE_SEC=1
export KEYCLOAK_ADMIN_PASSWORD=rehearsal-kc-admin ATLAS_KC_CI_SECRET=rehearsal-kc-ci OPENFGA_PRESHARED_KEY=rehearsal-fga
export ATLAS_MANAGER_BOOTSTRAP_KEY=atl_prod_rehearsalboot0000000000000_rehearsalonlysecret ATLAS_SECRET_REFRESH_S=5
export ATLAS_PUSH_ALLOW_HOSTS=host.docker.internal ATLAS_SANDBOX_TOKEN=rehearsal-sandbox-token
export ATLAS_KB_URL_ALLOW_HOSTS=fake-upstream ATLAS_EGRESS_ADMIN_TOKEN=rehearsal-egress-admin ATLAS_MKT_ADMIN_TOKEN=rehearsal-mkt-admin ATLAS_RPT_ADMIN_TOKEN=rehearsal-rpt-admin ATLAS_RPT_INGEST_SECRET=rehearsal-rpt-ingest
export ATLAS_SMTP_ADDR=mailpit:1025 ATLAS_SMTP_FROM=atlas-rehearsal@example.test ATLAS_NOTIFY_ALLOW_HOSTS=host.docker.internal
# 호스트 8080 이 다른 프로젝트에 점유돼 있으면 MANAGER_PORT 로 우회 (시험은 ATLAS_MANAGER_HOST_URL 을 따른다)
export MANAGER_PORT=${MANAGER_PORT:-8080} ATLAS_MANAGER_HOST_URL=http://127.0.0.1:${MANAGER_PORT:-8080}
make local-stop >/dev/null 2>&1
make prod-build >"$L/prod-build.txt" 2>&1 || { echo "FAIL prod-build"; tail -20 "$L/prod-build.txt"; exit 1; }
make db-migrate ENV=prod 2>&1 | tail -1
for _ in $(seq 1 150); do curl -fsS http://127.0.0.1:8007/readyz >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8005/readyz 2>/dev/null | grep -q guard-secret \
  && curl -fsS http://127.0.0.1:8024/readyz 2>/dev/null | grep -q '"registered":true' && curl -fsS http://127.0.0.1:8022/readyz 2>/dev/null | grep -q rpt-ingest-secret && curl -fsS http://127.0.0.1:8023/readyz 2>/dev/null | grep -q mcp-reg-admin-token && curl -fsS http://127.0.0.1:8019/readyz 2>/dev/null | grep -q '"registered":true' && curl -fsS http://127.0.0.1:8020/readyz 2>/dev/null | grep -q '"registered":true' && curl -fsS http://127.0.0.1:8000/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 2; done
for _ in $(seq 1 90); do docker inspect prodai-atlas-prod-keycloak-1 --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy && break; sleep 3; done
ATLAS_TEST_TOKEN=$(test/impl/14th/gatekey.py "$KEYCLOAK_ADMIN_PASSWORD" | tail -1); export ATLAS_TEST_TOKEN
rc=0
(cd agents/runtime && uv run --frozen python -W ignore ../../test/impl/28th/e28_iam.py prod) || rc=1
make prod-stop >/dev/null 2>&1
echo "== rc=$rc"; exit $rc
