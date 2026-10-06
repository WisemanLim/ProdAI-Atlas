#!/usr/bin/env bash
# E6-09 HITL 대기 중 al-run 재시작 → 체크포인트에서 재개 — 사용: e6-restart.sh local|prod
set -uo pipefail
m="${1:-local}"
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-$m -f docker-compose.yml -f docker-compose.$m.yml "${EXTRA[@]}" --env-file .env.$m)
R=http://127.0.0.1:8001
rpc() { curl -fsS -X POST $R/a2a -H 'content-type: application/json' -d "$1"; }
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
TID=$(rpc '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"parts":[{"data":{"goal":"atlas_delete_all 로 정리"}}]}}}' | j "d['result']['task']['id']")
s1=$(curl -fsS "$R/runs/$TID" | j "d['status']")
"${DC[@]}" restart agent-al-run >/dev/null 2>&1
for _ in $(seq 1 60); do curl -fsS -o /dev/null $R/readyz 2>/dev/null && break; sleep 1; done
OUT=$(rpc "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"SendMessage\",\"params\":{\"message\":{\"taskId\":\"$TID\",\"parts\":[{\"data\":{\"approval\":\"approve\"}}]}}}")
st=$(echo "$OUT" | j "d['result']['task']['status']['state']"); reason=$(echo "$OUT" | j "d['result']['task']['artifacts'][0]['parts'][0]['data']['reason']")
echo "before restart=$s1 after approve=$st reason=$reason"
[[ "$s1" == pending && "$st" == TASK_STATE_COMPLETED && "$reason" == END_TURN ]] && echo "PASS E6-09" || { echo "FAIL E6-09"; exit 1; }
