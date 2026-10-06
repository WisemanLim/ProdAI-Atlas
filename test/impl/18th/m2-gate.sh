#!/usr/bin/env bash
# M2 완료 게이트 — test-all + 레드팀 + prod(oidc·RLS·OpenBao) 에서 Epic 1~18 스모크·콘솔 SSO UI(v1·M2)·CLI SSO·RLS·OpenBao·A2A·다중 에이전트·승인함·알림 일괄.
# 인증 이전 스모크는 시험 shim(test/impl/14th/authshim)이 Manager 요청에 게이트 API 키를 붙인다. 리허설 시크릿만.
set -uo pipefail
cd "$(dirname "$0")/../../.."
L=test/impl/18th/logs; mkdir -p "$L/gate-ui" "$L/gate-ui-m2"
rc=0
echo "== make test-all"; make test-all >"$L/gate-test-all.txt" 2>&1 && echo "PASS test-all" || { echo "FAIL test-all"; rc=1; }
grep -E "^ok|passed|Tests " "$L/gate-test-all.txt"
echo "== guard-redteam"; make guard-redteam >"$L/gate-redteam.txt" 2>&1 && echo "PASS redteam" || { echo "FAIL redteam"; rc=1; }
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
export ATLAS_MCP_REG_ADMIN_TOKEN=rehearsal-mcp-admin ATLAS_GUARD_ADMIN_TOKEN=rehearsal-guard-admin ATLAS_GUARD_SECRET=rehearsal-guard-secret
export CLICKHOUSE_PASSWORD=rehearsal-clickhouse ATLAS_EV_CURATOR_TOKEN=rehearsal-ev-curator ATLAS_BATCH_POLL_BASE_SEC=1
export KEYCLOAK_ADMIN_PASSWORD=rehearsal-kc-admin ATLAS_KC_CI_SECRET=rehearsal-kc-ci OPENFGA_PRESHARED_KEY=rehearsal-fga
export ATLAS_MANAGER_BOOTSTRAP_KEY=atl_prod_rehearsalboot0000000000000_rehearsalonlysecret ATLAS_SECRET_REFRESH_S=5
export OPENAI_BASE_URL=http://gateway:8080/v1 OPENAI_API_KEYS=$ATLAS_GATEWAY_AL_PLAN_KEY
export ATLAS_PUSH_ALLOW_HOSTS=host.docker.internal ATLAS_SANDBOX_TOKEN=rehearsal-sandbox-token
export ATLAS_SMTP_ADDR=mailpit:1025 ATLAS_SMTP_FROM=atlas-rehearsal@example.test ATLAS_NOTIFY_ALLOW_HOSTS=host.docker.internal
make local-stop >/dev/null 2>&1
echo "== prod"; make prod-build >"$L/gate-prod-build.txt" 2>&1 || { echo "FAIL prod-build"; exit 1; }
make db-migrate ENV=prod 2>&1 | tail -1
make db-ping ENV=prod 2>&1 | tail -1
for _ in $(seq 1 120); do curl -fsS http://127.0.0.1:8001/readyz 2>/dev/null | grep -q '"registered":true' && curl -fsS http://127.0.0.1:8005/readyz 2>/dev/null | grep -q guard-secret \
  && curl -fsS -o /dev/null http://127.0.0.1:8007/readyz && curl -fsS -o /dev/null http://127.0.0.1:8006/readyz \
  && curl -fsS http://127.0.0.1:8014/readyz 2>/dev/null | grep -q sandbox-token && curl -fsS http://127.0.0.1:8010/readyz 2>/dev/null | grep -q '"registered":true' && break; sleep 2; done
for _ in $(seq 1 90); do docker inspect prodai-atlas-prod-keycloak-1 --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy && break; sleep 3; done
ATLAS_TEST_TOKEN=$(test/impl/14th/gatekey.py "$KEYCLOAK_ADMIN_PASSWORD" | tail -1); export ATLAS_TEST_TOKEN
SHIM="$PWD/test/impl/14th/authshim"
(
  export PYTHONPATH="$SHIM" PATH="$SHIM/bin:$PATH"
  make mcp-seed ENV=prod 2>&1 | grep -E "approve|allowlist"
  r=0
  test/impl/1st/e1-smoke.sh || r=1
  test/impl/2nd/e2_smoke.py || r=1
  test/impl/3rd/e3_smoke.py rehearsal-admin-token prod || r=1
  test/impl/4th/e4_trace.py prod http://127.0.0.1:8000 http://127.0.0.1:8090 "$ATLAS_GATEWAY_AL_PLAN_KEY" || r=1
  test/impl/5th/e5_e2e.py prod || r=1
  test/impl/6th/e6_loop.py prod || r=1
  test/impl/7th/e7_mcp.py prod || r=1
  test/impl/7th/e7-rugpull.sh prod rehearsal-mcp-admin || r=1
  test/impl/8th/e8_guard.py prod rehearsal-admin-token rehearsal-guard-admin "$ATLAS_GATEWAY_AL_PLAN_KEY" || r=1
  test/impl/9th/e9_ob.py prod "$ATLAS_GATEWAY_AL_PLAN_KEY" "$CLICKHOUSE_PASSWORD" || r=1
  test/impl/10th/e10-ev.sh prod "$ATLAS_GATEWAY_AL_PLAN_KEY" rehearsal-ev-curator || r=1
  make eval-seed ENV=prod >/dev/null 2>&1
  test/impl/11th/e11_http.py || r=1
  test/impl/13th/e13_econ.py rehearsal-admin-token http://127.0.0.1:8090 prodai-atlas-prod_default || r=1
  exit $r
) || rc=1
test/impl/14th/e14_iam.py prod "$KEYCLOAK_ADMIN_PASSWORD" "$ATLAS_KC_CI_SECRET" || rc=1
test/impl/14th/e14_rls.sh || rc=1
test/impl/14th/e14_console.py "$KEYCLOAK_ADMIN_PASSWORD" http://127.0.0.1:8080 "$L/gate-ui" || rc=1
test/impl/14th/e14_cli.py "$KEYCLOAK_ADMIN_PASSWORD" || rc=1
NEWVK=$(curl -s -X POST http://127.0.0.1:8090/admin/keys -H "Authorization: Bearer $ATLAS_GATEWAY_ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"tenant":"rotation","models":["*"]}' | python3 -c "import json,sys;print(json.load(sys.stdin)['key'])")
test/impl/15th/e15_secrets.py prod "$ATLAS_GATEWAY_ADMIN_TOKEN" "$NEWVK" || rc=1
test/impl/15th/e15_bao.sh || rc=1
(cd agents/runtime && uv run --frozen python ../../test/impl/16th/e16_a2a.py prod) || rc=1
(cd agents/runtime && uv run --frozen python ../../test/impl/17th/e17_ma.py prod) || rc=1
(cd agents/runtime && uv run --frozen python ../../test/impl/18th/e18_hitl_ntf.py prod) || rc=1
test/impl/18th/e18_console.py http://127.0.0.1:8080 "$L/gate-ui-m2" "$KEYCLOAK_ADMIN_PASSWORD" || rc=1
for c in manager gateway console agent-al-plan agent-al-run agent-mcp-reg agent-gr agent-ob agent-ev mcp-demo mcp-tool \
         agent-ma-orch agent-ma-merge agent-ma-res agent-ma-cr agent-ma-anl sandbox; do
  v=$(docker inspect "prodai-atlas-prod-$c-1" --format '{{.HostConfig.ReadonlyRootfs}} {{.Config.User}} {{.State.Status}}' 2>/dev/null)
  case "$v" in "true 0 "*|"") echo "FAIL hardening $c $v"; rc=1 ;; "true "*" running") echo "PASS hardening $c $v" ;; *) echo "FAIL hardening $c $v"; rc=1 ;; esac
done
make prod-stop >/dev/null 2>&1
echo "== gate rc=$rc"; exit $rc
