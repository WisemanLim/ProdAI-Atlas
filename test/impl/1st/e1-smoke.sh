#!/usr/bin/env bash
# Epic 1 Manager 카탈로그 스모크 — local·prod 공용. 사용: e1-smoke.sh [base_url] [nats_monitor_url]
set -uo pipefail
B="${1:-http://127.0.0.1:8080}"; N="${2:-http://127.0.0.1:8222}"
fail=0
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
check() { if [ "$2" = "$3" ]; then echo "PASS $1 ($2)"; else echo "FAIL $1 got=$2 want=$3"; fail=1; fi; }
M='{"apiVersion":"agents.prodai.io/v1alpha1","kind":"AtlasAgent","metadata":{"name":"smoke-plan","unitId":"AL-PLAN"},"spec":{"version":"0.1.0","interfaces":[{"binding":"JSONRPC","port":8000,"path":"/a2a"}]}}'
M2="${M/0.1.0/0.2.0}"
before=$(curl -fsS "$N/jsz?streams=true" | j "sum(s['state']['messages'] for a in d.get('account_details',[]) for s in a.get('stream_detail',[]) if s['name']=='PLT')")

check E1-03 "$(curl -fsS $B/api/v1/catalog/units | j "len(d['items'])")" 47
R=$(curl -sS -w '\n%{http_code}' -X POST $B/api/v1/agents -d "$M"); code=${R##*$'\n'}; body=${R%$'\n'*}
check E1-04 "$code:$(echo "$body" | j "d['layer']+'/'+str(d['currentRev'])")" "201:L1/1"
ID=$(echo "$body" | j "d['id']")
R=$(curl -sS -w '\n%{http_code}' -X POST $B/api/v1/agents -d '{"apiVersion":"agents.prodai.io/v1alpha1","kind":"AtlasAgent","metadata":{"name":"x","unitId":"NOPE"},"spec":{"version":"1.0.0","artifacts":{"image":"ghcr.io/a:latest"},"interfaces":[{"binding":"JSONRPC","port":1}]}}')
check E1-05 "${R##*$'\n'}:$(echo "${R%$'\n'*}" | j "d['error']+':'+','.join(sorted(x['field'] for x in d['details']))")" "422:invalid_manifest:metadata.unitId,spec.artifacts.image"
check E1-06 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/v1/agents -d "$M")" 409
check E1-07 "$(curl -fsS -X PATCH $B/api/v1/agents/$ID -d "$M" | j "str(d['changed'])+'/'+str(d['agent']['currentRev'])")" "False/1"
check E1-08 "$(curl -fsS -X PATCH $B/api/v1/agents/$ID -d "$M2" | j "str(d['changed'])+'/'+str(d['agent']['currentRev'])")/$(curl -fsS $B/api/v1/agents/$ID/versions | j "len(d['items'])")" "True/2/2"
d1=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE $B/api/v1/agents/$ID); g=$(curl -s -o /dev/null -w '%{http_code}' $B/api/v1/agents/$ID)
R=$(curl -sS -w '\n%{http_code}' -X POST $B/api/v1/agents -d "$M"); ID2=$(echo "${R%$'\n'*}" | j "d['id']")
check E1-09 "$d1/$g/${R##*$'\n'}" "204/404/201"
curl -s -o /dev/null -X DELETE $B/api/v1/agents/$ID2   # 정리 (+1 event)
sleep 1
after=$(curl -fsS "$N/jsz?streams=true" | j "sum(s['state']['messages'] for a in d.get('account_details',[]) for s in a.get('stream_detail',[]) if s['name']=='PLT')")
# registered + updated + deleted + registered + deleted = 5
check E1-10 "$((after-before))" 5
check E1-11 "$(curl -fsS $B/api/v1/openapi.json | j "d['openapi']")" 3.1.0
exit $fail
