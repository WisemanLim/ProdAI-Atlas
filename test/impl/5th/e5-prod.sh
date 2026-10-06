#!/usr/bin/env bash
# E5-09 prod 리허설 — 리허설 전용 키를 셸 env 로 주입
set -uo pipefail
export ATLAS_GATEWAY_ADMIN_TOKEN=rehearsal-admin-token
export ATLAS_GATEWAY_AL_PLAN_KEY=sk-atlas-prod-rehearsal0000000000000
make local-stop >/dev/null 2>&1
make prod-build >/dev/null 2>&1 || { echo "FAIL prod-build"; exit 1; }
make db-migrate ENV=prod 2>&1 | tail -1
rc=0
test/impl/5th/e5_e2e.py prod || rc=1
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars --window-size=1400,1300 \
  --virtual-time-budget=6000 --screenshot=test/impl/5th/logs/E5-09-console-prod.png http://127.0.0.1:5173/ >/dev/null 2>&1
make prod-stop >/dev/null 2>&1
exit $rc
