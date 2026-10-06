#!/usr/bin/env bash
# Epic 2 E2-08(재기동 no-op) · E2-09(fail-static) · E2-10(콜드 스타트) — local 스택에서 실행
set -uo pipefail
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-local -f docker-compose.yml -f docker-compose.local.yml "${EXTRA[@]}" --env-file .env.local)
A=http://127.0.0.1:8000; M=http://127.0.0.1:8080; fail=0
now() { python3 -c 'import time;print(time.time())'; }
rev() { curl -fsS "$M/api/v1/agents?unit=AL-PLAN" | python3 -c "import json,sys; print(json.load(sys.stdin)['items'][0]['currentRev'])"; }
reg() { curl -fsS $A/readyz 2>/dev/null | python3 -c "import json,sys; r=json.load(sys.stdin)['registration']; print(r['registered'], (r.get('error') or '')[:70])" 2>/dev/null; }
wait_ready() { for _ in $(seq 1 400); do curl -fsS -o /dev/null $A/readyz 2>/dev/null && return 0; sleep 0.05; done; return 1; }
wait_reg() { for _ in $(seq 1 240); do [ "$(reg | cut -d' ' -f1)" = True ] && return 0; sleep 0.5; done; return 1; }

echo "== E2-08 재기동 no-op"
r1=$(rev); "${DC[@]}" restart agent-al-plan >/dev/null 2>&1 || fail=1; wait_ready; wait_reg; r2=$(rev)
echo "rev before=$r1 after=$r2"; [ "$r1" = "$r2" ] && echo "PASS E2-08" || { echo "FAIL E2-08"; fail=1; }

echo "== E2-09 fail-static (Manager 중지)"
"${DC[@]}" stop manager >/dev/null 2>&1 || fail=1
"${DC[@]}" restart agent-al-plan >/dev/null 2>&1 || fail=1; wait_ready; sleep 2
st=$(reg); echo "registration: $st"
state=$(curl -fsS -X POST $A/a2a -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"parts":[{"text":"매니저 없이 계획"}]}}}' | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['task']['status']['state'])")
echo "a2a without manager: $state"
"${DC[@]}" start manager >/dev/null 2>&1 || fail=1; wait_reg; st2=$(reg); echo "after manager start: $st2"
if [[ "$st" == False* && "$state" == TASK_STATE_COMPLETED && "$st2" == True* ]]; then echo "PASS E2-09"; else echo "FAIL E2-09"; fail=1; fi

echo "== E2-10 콜드 스타트 (컨테이너 start → /readyz 200)"
for n in 1 2 3; do
  "${DC[@]}" stop agent-al-plan >/dev/null 2>&1
  t0=$(now); "${DC[@]}" start agent-al-plan >/dev/null 2>&1; wait_ready; t1=$(now)
  python3 -c "d=$t1-$t0; print('run $n: %.2fs' % d); raise SystemExit(0 if d<=5 else 1)" || fail=1
done
[ $fail = 0 ] && echo "PASS E2-10 (≤5s)" || echo "FAIL (see above)"
exit $fail
