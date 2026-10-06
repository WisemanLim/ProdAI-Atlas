#!/usr/bin/env bash
# E5-06 장애·복구 · E5-07 배포 중지 — 사용: e5-failover.sh local|prod
set -uo pipefail
m="${1:-local}"
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-$m -f docker-compose.yml -f docker-compose.$m.yml "${EXTRA[@]}" --env-file .env.$m)
M=http://127.0.0.1:8080; rc=0
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }
AID=$(curl -fsS "$M/api/v1/agents?unit=AL-PLAN" | j "d['items'][0]['id']")
dep_status() { curl -fsS "$M/api/v1/deployments?agentId=$AID" | j "next((x['status'] for x in d['items'] if x['status']!='stopped'), 'none')"; }
dep_id() { curl -fsS "$M/api/v1/deployments?agentId=$AID" | j "next((x['id'] for x in d['items'] if x['status']!='stopped'), '')"; }
wait_status() { for _ in $(seq 1 60); do [ "$(dep_status)" = "$1" ] && return 0; sleep 1; done; return 1; }
proxy_code() { curl -s -o /dev/null -w '%{http_code}' -X POST "$M/api/v1/agents/$AID/a2a" -d '{"jsonrpc":"2.0","id":1,"method":"ListTasks","params":{}}'; }

echo "== E5-06 장애·복구"
"${DC[@]}" stop agent-al-plan >/dev/null 2>&1
seen=""; for _ in $(seq 1 60); do s=$(dep_status); [[ "$seen" == *"$s"* ]] || seen="$seen $s"; [ "$s" = failed ] && break; sleep 1; done
echo "observed:$seen"; pc=$(proxy_code); echo "proxy while failed: $pc"
"${DC[@]}" start agent-al-plan >/dev/null 2>&1
wait_status ready && back=ready || back=$(dep_status); echo "after restart: $back"; pc2=$(proxy_code)
if [[ "$seen" == *degraded* && "$seen" == *failed* && "$pc" == 503 && "$back" == ready && "$pc2" == 200 ]]; then echo "PASS E5-06"; else echo "FAIL E5-06"; rc=1; fi

echo "== E5-07 배포 중지"
DID=$(dep_id); stopped=$(curl -fsS -X DELETE "$M/api/v1/deployments/$DID" | j "d['status']"); pc=$(proxy_code)
"${DC[@]}" restart agent-al-plan >/dev/null 2>&1
wait_status ready && new=$(dep_id) || new=""
echo "stopped=$stopped proxy=$pc new_deployment=${new:0:8} (old ${DID:0:8})"
if [[ "$stopped" == stopped && "$pc" == 503 && -n "$new" && "$new" != "$DID" ]]; then echo "PASS E5-07"; else echo "FAIL E5-07"; rc=1; fi
exit $rc
