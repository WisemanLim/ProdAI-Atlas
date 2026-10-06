#!/usr/bin/env bash
# E12-07 서명 리허설: 로컬 registry:2 + cosign 키쌍 (CI 는 keyless). 키·비밀번호는 리허설 전용 임시값.
set -uo pipefail
cd "$(dirname "$0")/../../.."
REG="${REG:-localhost:5555}"; K=$(mktemp -d); trap 'rm -rf $K' EXIT
cos() { docker run --rm --network host -v "$K:/k" -v "$PWD/dist/sbom:/sbom" -e COSIGN_PASSWORD=rehearsal-only-not-secret gcr.io/projectsigstore/cosign:v2.4.1 "$@"; }
REF=$REG/atlas-gateway; D=$(docker buildx imagetools inspect $REF:0.2.0 | awk '/^Digest:/{print $2}')
X=(--allow-insecure-registry); V=(--insecure-ignore-tlog=true "${X[@]}")
pass=0; fail=0; chk() { if [ "$2" = "$3" ]; then echo "PASS $1 got=$2"; pass=$((pass+1)); else echo "FAIL $1 got=$2 want=$3"; fail=$((fail+1)); fi; }
cos generate-key-pair --output-key-prefix /k/atlas >/dev/null 2>&1; cos generate-key-pair --output-key-prefix /k/other >/dev/null 2>&1
cos sign --yes --key /k/atlas.key --tlog-upload=false "${X[@]}" $REF@$D >/dev/null 2>&1; chk sign $? 0
cos attest --yes --key /k/atlas.key --tlog-upload=false "${X[@]}" --type cyclonedx --predicate /sbom/atlas-gateway.cdx.json $REF@$D >/dev/null 2>&1; chk attest $? 0
cos verify --key /k/atlas.pub "${V[@]}" $REF@$D >/dev/null 2>&1; chk verify-ok $? 0
n=$(cos verify-attestation --key /k/atlas.pub "${V[@]}" --type cyclonedx $REF@$D 2>/dev/null | head -1 | python3 -c "import sys,json,base64;d=json.loads(sys.stdin.read());p=json.loads(base64.b64decode(d['payload']));print(p['predicateType'].endswith('cyclonedx') or 'cyclonedx' in p['predicateType'], len(p['predicate'].get('components',[]))>0)")
chk verify-attestation "$n" "True True"
cos verify --key /k/other.pub "${V[@]}" $REF@$D >/dev/null 2>&1; chk verify-wrong-key $([ $? -ne 0 ] && echo rejected) rejected
cos verify --key /k/atlas.pub "${V[@]}" $REG/atlas-console:0.2.0 >/dev/null 2>&1; chk verify-unsigned $([ $? -ne 0 ] && echo rejected) rejected
echo "cosign: pass=$pass fail=$fail"; [ $fail -eq 0 ]
