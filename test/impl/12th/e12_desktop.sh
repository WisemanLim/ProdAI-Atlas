#!/usr/bin/env bash
# E12-12 데스크톱 macOS: .app 실행 → 사이드카 /health · 콘솔 서빙 → 정상 종료 시 사이드카 종료
set -uo pipefail
cd "$(dirname "$0")/../../.."
APP="apps/desktop/src-tauri/target/release/bundle/macos/ProdAI Atlas.app"
LOG=$(mktemp); pass=0; fail=0
chk() { if [ "$2" = "$3" ]; then echo "PASS $1 got=$2"; pass=$((pass+1)); else echo "FAIL $1 got=$2 want=$3"; fail=$((fail+1)); fi; }
chk bundle "$(test -x "$APP/Contents/MacOS/atlas-desktop" && test -x "$APP/Contents/MacOS/atlas-manager" && echo ok)" ok
chk signature "$(codesign -dv "$APP" 2>&1 | sed -n 's/^Signature=//p')" adhoc
"$APP/Contents/MacOS/atlas-desktop" >"$LOG" 2>&1 &
APID=$!
for _ in $(seq 1 100); do grep -q "manager ready on" "$LOG" && break; sleep 0.2; done
PORT=$(sed -n 's/.*manager ready on 127.0.0.1:\([0-9]*\).*/\1/p' "$LOG" | head -1)
echo "port=$PORT"
H=$(curl -fsS "http://127.0.0.1:$PORT/health")
chk health "$(echo "$H" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'],d['env'],d['db']['engine'],d['version'])")" "ok desktop sqlite 0.2.0"
chk console "$(curl -fsS "http://127.0.0.1:$PORT/o/default/p/default/e/dev/overview" | grep -c '<div id="root">')" 1
chk api "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/agents")" 200
chk loopback-only "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | awk 'NR>1{print $9}' | sort -u)" "127.0.0.1:$PORT"
SPID=$(pgrep -f "Contents/MacOS/atlas-manager" | head -1)
chk sidecar-parent "$(ps -o ppid= -p "$SPID" | tr -d ' ')" "$APID"
DB="$HOME/Library/Application Support/io.prodai.atlas/atlas.db"
chk app-data-db "$(test -s "$DB" && echo ok)" ok
osascript -e 'tell application id "io.prodai.atlas" to quit' >/dev/null 2>&1
for _ in $(seq 1 50); do kill -0 $APID 2>/dev/null || break; sleep 0.2; done
sleep 0.5
chk app-exited "$(kill -0 $APID 2>/dev/null && echo running || echo exited)" exited
chk sidecar-cleaned "$(pgrep -f "Contents/MacOS/atlas-manager" >/dev/null && echo leaked || echo cleaned)" cleaned
echo "--- app log (head)"; head -5 "$LOG"; rm -f "$LOG"
echo "desktop: pass=$pass fail=$fail"; [ $fail -eq 0 ]
