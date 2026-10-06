#!/usr/bin/env bash
# E5-10 M0 게이트 — agents/runtime 단독 실행 (자기 디렉터리의 wds 표준 compose, Manager·Gateway 없음)
set -uo pipefail
cd agents/runtime
make local-build >/dev/null 2>&1 || { echo "FAIL local-build"; exit 1; }
for _ in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:8000/readyz 2>/dev/null && break; sleep 1; done
python3 - <<'PY'
import json, sys, urllib.request
card = json.load(urllib.request.urlopen("http://127.0.0.1:8000/.well-known/agent-card.json"))
ready = json.load(urllib.request.urlopen("http://127.0.0.1:8000/readyz"))
req = urllib.request.Request("http://127.0.0.1:8000/a2a", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
      "params": {"message": {"parts": [{"text": "단독으로 계획한다. 그리고 확인한다"}]}}}).encode(), {"content-type": "application/json"})
task = json.load(urllib.request.urlopen(req))["result"]["task"]
got = (card["name"], ready["status"], ready["registration"]["enabled"], task["status"]["state"], task["artifacts"][0]["parts"][0]["data"]["source"])
want = ("al-plan", "ok", False, "TASK_STATE_COMPLETED", "heuristic")
print(("PASS" if got == want else "FAIL") + f" E5-10 got={got!r}")
sys.exit(got != want)
PY
rc=$?
make local-stop >/dev/null 2>&1
exit $rc
