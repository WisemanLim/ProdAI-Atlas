#!/usr/bin/env bash
# E12-08 채널 승격(digest 재태깅) · E12-09 라이선스 정책 거부. 로컬 registry:2 사용.
set -uo pipefail
cd "$(dirname "$0")/../../.."
export ATLAS_REGISTRY="${REG:-localhost:5555}"; T=$(mktemp -d); trap 'rm -rf $T' EXIT
export CHANNELS_FILE=$T/channels.json
pass=0; fail=0; chk() { if [ "$2" = "$3" ]; then echo "PASS $1 got=$2"; pass=$((pass+1)); else echo "FAIL $1 got=$2 want=$3"; fail=$((fail+1)); fi; }
dg() { docker buildx imagetools inspect "$ATLAS_REGISTRY/$1" | awk '/^Digest:/{print $2}'; }
python3 scripts/channels.py set nightly 0.2.0 >/dev/null; chk set-nightly $? 0
python3 scripts/channels.py promote nightly stable >/dev/null 2>&1; chk skip-beta-rejected $([ $? -ne 0 ] && echo rejected) rejected
python3 scripts/channels.py promote nightly beta >/dev/null 2>&1; chk promote-beta $? 0
python3 scripts/channels.py promote beta stable >/dev/null 2>&1; chk promote-stable $? 0
same=yes; for i in atlas-manager atlas-gateway atlas-console atlas-agent-runtime; do [ "$(dg $i:stable)" = "$(dg $i:0.2.0)" ] || same=no; done
chk stable-digest-equals-build $same yes
chk channels-json "$(python3 -c "import json;d=json.load(open('$CHANNELS_FILE'));c=d['channels'];print(c['stable']['version'],c['stable']['promotedFrom'],c['stable']['digests']==c['nightly']['digests'])")" "0.2.0 beta True"
# 라이선스 정책: AGPL 라이브러리 → 거부, OS 패키지 GPL → 허용
cat > $T/bad.cdx.json <<'J'
{"components":[{"name":"evil","version":"1","purl":"pkg:pypi/evil@1","licenses":[{"license":{"id":"AGPL-3.0-only"}}]},
 {"name":"bash","version":"5","purl":"pkg:apk/alpine/bash@5","licenses":[{"license":{"id":"GPL-3.0-or-later"}}]}]}
J
python3 scripts/license-check.py $T/bad.cdx.json >/dev/null; chk license-agpl-rejected $? 1
python3 scripts/license-check.py dist/sbom/*.cdx.json >/dev/null; chk license-real-sboms $? 0
echo "release: pass=$pass fail=$fail"; [ $fail -eq 0 ]
