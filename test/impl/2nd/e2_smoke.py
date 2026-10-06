#!/usr/bin/env python3
"""Epic 2 Agent SDK 스모크 — local·prod 공용. 사용: e2_smoke.py [agent_url] [manager_url]"""
import json
import sys
import urllib.request

A = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
M = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
failed = 0


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def rpc(method, params, rid=1):
    req = urllib.request.Request(f"{A}/a2a", json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode(),
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def check(cid, got, want):
    global failed
    ok = got == want
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got!r}" + ("" if ok else f" want={want!r}"))


REQUIRED = {"name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills"}
card = get(f"{A}/.well-known/agent-card.json")
check("E2-03", (len(REQUIRED & card.keys()), card["supportedInterfaces"][0]["url"]), (8, "http://agent-al-plan:8000/a2a"))

task = rpc("SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "요구사항 정리. 그리고 구현. 그다음 테스트"}]}})["result"]["task"]
plan = task["artifacts"][0]["parts"][0]["data"]
check("E2-04", (task["status"]["state"], len(plan["steps"]), bool(plan["dod"])), ("TASK_STATE_COMPLETED", 3, True))

got = rpc("GetTask", {"id": task["id"]})["result"]["id"]
listed = [t["id"] for t in rpc("ListTasks", {"pageSize": 100})["result"]["tasks"]]
check("E2-05", (got == task["id"], task["id"] in listed), (True, True))

ta = rpc("SendMessage", {"tenant": "org-a", "message": {"parts": [{"text": "테넌트 A"}]}})["result"]["task"]["id"]
check("E2-06", rpc("GetTask", {"tenant": "org-b", "id": ta})["error"]["code"], -32001)

names = [a["name"] for a in get(f"{M}/api/v1/agents?unit=AL-PLAN")["items"]]
check("E2-07", (names, get(f"{A}/readyz")["registration"]["registered"]), (["al-plan"], True))
sys.exit(failed)
