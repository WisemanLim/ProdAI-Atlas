#!/bin/sh
# OpenBao 초기화·봉인 해제·정책·서비스 토큰 (prod compose 1회성 컨테이너, FR-PLT-SEC-01·02 리허설 수준).
# - 최초: init(키 1조각) → unseal → KV v2(atlas) → 정책(atlas-manager·atlas-gateway) → 주기 토큰 → 루트 토큰 폐기
# - 재기동: 봉인돼 있으면 보관한 unseal 키로 해제
# 한계: unseal 키를 같은 호스트 볼륨(baokeys, 0600)에 둔다 — 운영은 KMS/Transit auto-unseal(SEC-02, P1)로 바꿔야 한다.
set -eu
export BAO_ADDR=${BAO_ADDR:-http://openbao:8200}
K=/keys; T=/tokens
for i in $(seq 1 60); do rc=0; bao status >/dev/null 2>&1 || rc=$?; [ $rc -ne 1 ] && break; sleep 1; done   # rc 1 = 연결 실패, 2 = 봉인·미초기화, 0 = 해제
if ! bao status -format=json 2>/dev/null | grep -q '"initialized": true'; then
  echo "openbao: init"
  bao operator init -key-shares=1 -key-threshold=1 > $K/init.txt
  chmod 600 $K/init.txt
  awk '/Unseal Key 1:/{print $4}' $K/init.txt > $K/unseal; chmod 600 $K/unseal
  ROOT=$(awk '/Initial Root Token:/{print $4}' $K/init.txt)
  sed -i '/Initial Root Token/d' $K/init.txt          # 루트 토큰은 파일에 남기지 않는다
  bao operator unseal "$(cat $K/unseal)" >/dev/null
  export BAO_TOKEN=$ROOT
  bao secrets enable -path=atlas -version=2 kv >/dev/null
  cat > /tmp/manager.hcl <<'P'
path "atlas/data/orgs/*"     { capabilities = ["create", "read", "update", "delete"] }
path "atlas/metadata/orgs/*" { capabilities = ["list", "read", "delete"] }
path "atlas/metadata/orgs"   { capabilities = ["list"] }
path "auth/token/renew-self" { capabilities = ["update"] }
P
  cat > /tmp/gateway.hcl <<'P'
path "atlas/data/providers/*"     { capabilities = ["create", "read", "update"] }
path "atlas/metadata/providers/*" { capabilities = ["list", "read"] }
path "auth/token/renew-self"      { capabilities = ["update"] }
P
  bao policy write atlas-manager /tmp/manager.hcl >/dev/null
  bao policy write atlas-gateway /tmp/gateway.hcl >/dev/null
  bao token create -orphan -policy=atlas-manager -period=768h -display-name=atlas-manager -field=token > $T/manager
  bao token create -orphan -policy=atlas-gateway -period=768h -display-name=atlas-gateway -field=token > $T/gateway
  chmod 0444 $T/manager $T/gateway                    # 볼륨은 manager·gateway 에만 읽기 전용으로 붙는다
  bao token revoke -self >/dev/null                   # 초기 루트 토큰 즉시 폐기
  unset BAO_TOKEN
  echo "openbao: initialized (kv atlas, policies, service tokens, root revoked)"
elif bao status -format=json | grep -q '"sealed": true'; then
  bao operator unseal "$(cat $K/unseal)" >/dev/null && echo "openbao: unsealed"
else
  echo "openbao: ready"
fi
