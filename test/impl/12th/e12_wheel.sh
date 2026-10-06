#!/usr/bin/env bash
# E12-04 wheel(prodai-agents): uv build → 깨끗한 python:3.12-slim 에 설치 → atlas-agent serve 단독 기동 → Card·readyz·SendMessage
set -uo pipefail
cd "$(dirname "$0")/../../.."
W=$(mktemp -d); trap 'rm -rf $W' EXIT
(cd agents/runtime && uv build --wheel --out-dir "$W" >/dev/null 2>&1) || { echo "FAIL build"; exit 1; }
ls "$W"
docker run --rm -v "$W:/w:ro" python:3.12-slim sh -c '
  pip install -q --no-cache-dir /w/*.whl 2>&1 | grep -v "notice\|WARNING: Running pip" ;
  atlas-agent units | head -3 ;
  (atlas-agent serve --unit units.al_plan --port 8000 --db sqlite:////tmp/p.db >/tmp/s.log 2>&1 &) ;
  python - <<"P"
import json, time, urllib.request as u
B = "http://127.0.0.1:8000"
for _ in range(100):
    try:
        r = json.load(u.urlopen(B + "/readyz")); break
    except Exception: time.sleep(0.1)
card = json.load(u.urlopen(B + "/.well-known/agent-card.json"))
req = u.Request(B + "/a2a", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
      "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "요구사항 정리. 그리고 구현"}]}}}).encode(),
      {"Content-Type": "application/json"})
task = json.load(u.urlopen(req))["result"]["task"]
ok = card.get("name") and r.get("version") and task["status"]["state"] == "TASK_STATE_COMPLETED"
print(("PASS" if ok else "FAIL"), "E12-04 card=%s ready.version=%s agent_core=%s state=%s" % (card.get("name"), r.get("version"), r.get("agent_core"), task["status"]["state"]))
P'
