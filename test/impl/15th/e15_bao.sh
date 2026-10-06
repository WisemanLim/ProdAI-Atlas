#!/usr/bin/env bash
# E15-07 OpenBao 운영 성질 — 초기화·루트 토큰 폐기·서비스 간 최소 권한·재기동 후 봉인 해제 복구 (prod 리허설)
set -uo pipefail
cd "$(dirname "$0")/../../.."
DC=(test/impl/14th/dc.sh prod)
pass=0; fail=0; chk() { if [ "$2" = "$3" ]; then echo "PASS $1 got=$2"; pass=$((pass+1)); else echo "FAIL $1 got=$2 want=$3"; fail=$((fail+1)); fi; }
B() { docker exec -e BAO_ADDR=http://127.0.0.1:8200 prodai-atlas-prod-openbao-1 bao "$@"; }
st=$(B status -format=json); chk initialized "$(echo "$st" | grep -c '"initialized": true')" 1; chk unsealed "$(echo "$st" | grep -c '"sealed": false')" 1
chk root-token-not-stored "$(docker run --rm -v prodai-atlas-prod_baokeys:/k alpine sh -c 'grep -c "Root Token" /k/init.txt; true')" 0
MT=$(docker exec prodai-atlas-prod-manager-1 cat /run/secrets/atlas/bao/manager); GT=$(docker exec prodai-atlas-prod-gateway-1 cat /run/secrets/atlas/bao/gateway)
chk manager-policy "$(docker exec -e BAO_TOKEN="$MT" -e BAO_ADDR=http://127.0.0.1:8200 prodai-atlas-prod-openbao-1 bao token lookup -format=json | grep -o '"atlas-manager"' | head -1)" '"atlas-manager"'
chk manager-cannot-read-provider-keys "$(docker exec -e BAO_TOKEN="$MT" -e BAO_ADDR=http://127.0.0.1:8200 prodai-atlas-prod-openbao-1 bao kv get atlas/providers/openai 2>&1 | grep -c 'permission denied')" 1
chk gateway-cannot-read-org-secrets "$(docker exec -e BAO_TOKEN="$GT" -e BAO_ADDR=http://127.0.0.1:8200 prodai-atlas-prod-openbao-1 bao kv get atlas/orgs/default/guard-secret 2>&1 | grep -c 'permission denied')" 1
chk token-file-readonly-mount "$(docker exec prodai-atlas-prod-manager-1 sh -c 'echo x > /run/secrets/atlas/bao/manager' 2>&1 | grep -ciE 'read-only|permission denied')" 1
# 재기동 → 봉인 → 비밀 API 실패 → openbao-init 재실행으로 복구
docker restart prodai-atlas-prod-openbao-1 >/dev/null
for _ in $(seq 1 30); do B status >/dev/null 2>&1; [ $? -eq 2 ] && break; sleep 1; done
chk sealed-after-restart "$(B status -format=json | grep -c '"sealed": true')" 1
H=(-H "Authorization: Bearer ${ATLAS_TEST_TOKEN:-}")
chk api-fails-while-sealed "$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" http://127.0.0.1:8080/api/v1/orgs/default/secrets)" 502
"${DC[@]}" up -d --no-deps openbao-init >/dev/null 2>&1; sleep 3
chk unsealed-by-init "$(B status -format=json | grep -c '"sealed": false')" 1
chk api-recovers "$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" http://127.0.0.1:8080/api/v1/orgs/default/secrets)" 200
echo "bao: pass=$pass fail=$fail"; [ $fail -eq 0 ]
