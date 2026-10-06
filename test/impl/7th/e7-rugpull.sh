#!/usr/bin/env bash
# E7-08 rug pull 차단 · E7-09 오염 서버 등록 거부 — 사용: e7-rugpull.sh local|prod <reg_admin_token>
set -uo pipefail
m="${1:-local}"; TOKEN="$2"; rc=0
EXTRA=(); for f in compose/*.yml; do EXTRA+=(-f "$f"); done   # compose/*.yml 전부 (Makefile 과 동일)
DC=(docker compose -p prodai-atlas-$m -f docker-compose.yml -f docker-compose.$m.yml "${EXTRA[@]}" --env-file .env.$m)
M=http://127.0.0.1:8080; R=http://127.0.0.1:8001; G=http://127.0.0.1:8002
DEMO_POISON=true "${DC[@]}" up -d mcp-demo >/dev/null 2>&1; sleep 4
AID=$(curl -fsS "$M/api/v1/agents?unit=AL-RUN" | python3 -c "import json,sys; print(json.load(sys.stdin)['items'][0]['id'])")
TID=$(curl -fsS -X POST "$M/api/v1/agents/$AID/a2a" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"parts":[{"data":{"goal":"demo__search_docs 로 atlas 검색"}}]}}}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['task']['id'])")
curl -fsS "$R/runs/$TID/events?follow=false" | python3 -c "
import sys, json
evs=[json.loads(l[6:]) for l in sys.stdin.read().splitlines() if l.startswith('data: ')]
mcp=next(e for e in evs if 'blocked' in e); sel=next((e for e in evs if 'selected' in e), {'selected':[]})
blocked={b['tool']:b['reason'] for b in mcp['blocked']}
ok = blocked.get('demo__search_docs')=='pin_mismatch' and 'demo__search_docs' not in sel['selected']
print(('PASS' if ok else 'FAIL')+' E7-08 blocked=%s selected=%s' % (blocked, sel['selected'])); sys.exit(not ok)" || rc=1
curl -fsS -X POST $G/admin/servers -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"io.evil/poisoned","version":"0.1.0","remotes":[{"type":"streamable-http","url":"http://mcp-demo:8000/mcp"}]}' \
  | python3 -c "
import sys, json
d=json.load(sys.stdin); st=d['_meta']['io.prodai.atlas/registry']['status']; sc=d['scan']['score']
ok = st=='rejected' and sc>=70
print(('PASS' if ok else 'FAIL')+' E7-09 status=%s score=%s rules=%s' % (st, sc, sorted({f['rule'] for fs in d['scan']['findings'].values() for f in fs}))); sys.exit(not ok)" || rc=1
DEMO_POISON=false "${DC[@]}" up -d mcp-demo >/dev/null 2>&1   # 원복
exit $rc
