#!/usr/bin/env bash
# Epic 10 E2E — 사용: e10-ev.sh local|prod <bootstrap_key> <curator_token>
set -uo pipefail
m="${1:-local}"; KEY="$2"; CUR="$3"; rc=0
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-$m -f docker-compose.yml -f docker-compose.$m.yml "${EXTRA[@]}" --env-file .env.$m)
EV=http://127.0.0.1:8007; G=http://127.0.0.1:8090; OB=http://127.0.0.1:8006
pass() { echo "PASS $1 $2"; }; fail() { echo "FAIL $1 $2"; rc=1; }
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
NONCE=$(python3 -c 'import secrets;print(secrets.token_hex(3))')
DS="planner-golden"

# 정리: 이전 실행(impl 10th round 1)이 planner-golden 에 섞은 비계획 형식 항목을 retire (승격 입력 검증 도입 전 잔여)
for iid in $(curl -fsS "$EV/v1/datasets/$DS/items" 2>/dev/null | python3 -c "import json,sys; print(' '.join(i['id'] for i in json.load(sys.stdin)['items'] if i['status']!='retired' and 'goal' not in (i['input'] or {})))" 2>/dev/null); do
  curl -fsS -o /dev/null -X POST "$EV/v1/items/$iid/transition" -H 'content-type: application/json' -d '{"to":"retired","critique":"형식 혼입 정리"}' && echo "retired legacy item $iid"
done

# E10-02 시드 (재실행 시 기존 데이터셋에 항목 추가 + 새 버전)
out=$(make eval-seed ENV=$m 2>&1 | grep -E "bulk|snapshot")
echo "$out" | grep -q "'accepted': 3" && echo "$out" | grep -q "'version'" && pass E10-02 "$(echo "$out" | tr '\n' ' ')" || fail E10-02 "$out"

# E10-03 게이트 통과
out=$(make eval ENV=$m 2>&1); code=$?
line=$(echo "$out" | grep "gate=")
[[ $code == 0 && "$line" == *"gate=PASS"* && "$line" == *"pass^k=1.0"* ]] && pass E10-03 "$line" || fail E10-03 "code=$code $out"

# E10-04 게이트 실패 / E10-05 오류
"${DC[@]}" exec -T agent-ev sh -c "sed 's/pattern: .\"steps\"./pattern: \"IMPOSSIBLE_$NONCE\"/' evals/planner-smoke.yaml > /tmp/strict.yaml && sed 's/planner-golden/no-such-$NONCE/' evals/planner-smoke.yaml > /tmp/missing.yaml"
"${DC[@]}" exec -T agent-ev python -m units.ev.cli run --suite /tmp/strict.yaml --ci > /tmp/e10-strict.txt 2>&1; c4=$?
"${DC[@]}" exec -T agent-ev python -m units.ev.cli run --suite /tmp/missing.yaml --ci > /tmp/e10-missing.txt 2>&1; c5=$?
[[ $c4 == 100 ]] && grep -q "gate=FAIL" /tmp/e10-strict.txt && pass E10-04 "exit=$c4 $(grep -m1 '^  -' /tmp/e10-strict.txt)" || fail E10-04 "exit=$c4 $(cat /tmp/e10-strict.txt)"
[[ $c5 == 2 ]] && pass E10-05 "exit=$c5" || fail E10-05 "exit=$c5 $(cat /tmp/e10-missing.txt)"

# E10-06 trace → 골든셋 (계획 골든셋과 형식이 다른 상담 골든셋으로 승격)
SDS="support-golden"
curl -s -o /dev/null -X POST "$EV/v1/datasets" -H 'content-type: application/json' -d "{\"name\":\"$SDS\",\"input_schema\":{\"type\":\"object\",\"required\":[\"prompt\"]}}"
TID=$(curl -fsS -D - -o /dev/null $G/v1/chat/completions -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d "{\"model\":\"chat-default\",\"messages\":[{\"role\":\"user\",\"content\":\"고객 010-2222-3333 환불 문의 $NONCE\"}]}" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-atlas-trace-id"{print $2}')
# impl 15th 갱신: 가드 span 이 먼저 수집돼 /io 가 200 이어도 Gateway chat span 이 아직 없을 수 있다 → 본문 수집(captured)까지 대기
for _ in $(seq 1 30); do curl -fsS "$OB/v1/traces/$TID/io" 2>/dev/null | grep -q '"captured":true' && break; sleep 1; done
c422=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$EV/v1/datasets/$DS/items:from-trace" -H 'content-type: application/json' -d "{\"trace_id\":\"$TID\"}")
echo "planner-golden 승격 시도(형식 불일치) → $c422 (기대 422)"; [[ $c422 == 422 ]] || rc=1
ITEM=$(curl -fsS -X POST "$EV/v1/datasets/$SDS/items:from-trace" -H 'content-type: application/json' -d "{\"trace_id\":\"$TID\"}")
IID=$(echo "$ITEM" | j "d['id']")
echo "$ITEM" | python3 -c "
import json,sys; d=json.load(sys.stdin); ok=d['status']=='in_review' and d['source']=='prod-trace' and d['source_trace_id']=='$TID' and '010-2222-3333' not in json.dumps(d,ensure_ascii=False) and '***-****-3333' in d['input']['prompt']
print(('PASS' if ok else 'FAIL')+' E10-06 '+json.dumps({k:d[k] for k in ('status','source','input')},ensure_ascii=False)); sys.exit(not ok)" || rc=1

# E10-07 검수
c1=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$EV/v1/items/$IID/transition" -H 'content-type: application/json' -d '{"to":"approved"}')
LAB=$(curl -fsS -X POST "$EV/v1/items/$IID/transition" -H "authorization: Bearer $CUR" -H 'content-type: application/json' -d '{"to":"approved","verdict":"pass","critique":"E10 승격 검수"}')
st=$(echo "$LAB" | j "d['status']+'/'+','.join(l['actor'] for l in d['labels'])")
[[ $c1 == 403 && "$st" == "approved/curator" ]] && pass E10-07 "$c1 → $st" || fail E10-07 "$c1 $st"

# E10-08 불변성·새 버전
c2=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH "$EV/v1/items/$IID" -H 'content-type: application/json' -d '{"tags":["x"]}')
V=$(curl -fsS -X POST "$EV/v1/datasets/$SDS/versions" | j "d['version']")
inV=$(curl -fsS "$EV/v1/datasets/$SDS/versions/$V" | python3 -c "import json,sys; print('$IID' in [i['item_id'] for i in json.load(sys.stdin)['items']])")
in1=$( [[ $V -gt 1 ]] && curl -fsS "$EV/v1/datasets/$SDS/versions/$((V-1))" | python3 -c "import json,sys; print('$IID' in [i['item_id'] for i in json.load(sys.stdin)['items']])" || echo False)
[[ $c2 == 409 && $inV == True && $in1 == False ]] && pass E10-08 "patch=$c2 v$V has item, 이전 버전 불변" || fail E10-08 "$c2 $inV $in1"

# E10-09 비용 귀속
sleep 3
cost=$(curl -fsS "$OB/v1/costs?group_by=agent&since=3600" | python3 -c "import json,sys; print(next((r['cost_usd'] for r in json.load(sys.stdin)['items'] if r['grp']=='EV-REG'),0))")
python3 -c "import sys; sys.exit(0 if float('$cost')>0 else 1)" && pass E10-09 "EV-REG cost=$cost" || fail E10-09 "cost=$cost"
exit $rc
