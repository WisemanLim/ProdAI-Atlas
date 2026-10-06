"""Epic 17 다중 에이전트 스택 시험 — 오케스트레이터(MA-ORCH)를 Manager 경유로 호출하고 서브에이전트·샌드박스·병합을 검증.
실행: cd agents/runtime && uv run --frozen python ../../test/impl/17th/e17_ma.py <local|prod>"""
import asyncio
import json
import os
import sys

import httpx

from atlas_agent_core import a2a_client

M = "http://127.0.0.1:8080"
TOKEN = os.environ.get("ATLAS_TEST_TOKEN", "")
failed = 0


def check(cid, got, want):
    global failed
    ok = got == want
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {cid} got={got}" + ("" if ok else f" want={want}"), flush=True)


async def tok():
    return TOKEN or None


cl = a2a_client.A2AClient(M, tok)
DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,2 +10,5 @@ def q():
 x = 1
+API_KEY = "sk-live-1234567890"
+cur.execute("SELECT * FROM t WHERE id=" + uid)
+print(x)
"""
CSV = "region,sales\nseoul,120\nbusan,80\nseoul,30\ndaegu,50\n"


async def main():
    ags = {a["name"]: a["agentId"] for a in await cl.search(limit=50)}
    orch = ags["ma-orch"]

    # E17-01 복잡 과업: 조사·리뷰·분석 3 관점 병렬 위임 → synthesis 병합 (ORCH-01·03·04, MERGE-03)
    goal = "변경 diff 를 보안 관점에서 리뷰하고, 매출 데이터를 분석하고, 다중 에이전트 조정 비용이 왜 병목인지 문서를 조사해 함께 정리"
    r = await cl.delegate({"goal": goal, "inputs": {"diff": DIFF, "data": CSV}, "dod": [{"type": "test", "spec": "contains:hardcoded-secret"}]},
                          agent=orch, skill="ma.orchestrate", timeout=120)
    o = r.get("output") or {}
    sub = o.get("subtasks", [])
    kinds = sorted({s["spec"]["kind"] for s in sub})
    rep = (o.get("merged") or {}).get("report", "")
    check("E17-01", (r["state"], o.get("mode"), o.get("complexity"), kinds, all(s.get("state") == "TASK_STATE_COMPLETED" for s in sub),
                     (o.get("merged") or {}).get("strategy"), "hardcoded-secret" in rep, "#p" in rep, "sales.mean" in rep, (o.get("merged") or {}).get("verified")),
          ("TASK_STATE_COMPLETED", "multi", "complex", ["analysis", "research", "review"], True, "synthesis", True, True, True, True))

    # E17-02 위임 명세 완전성 (ORCH-02) · 조정 비용 지표 (ORCH-07) · 쓰기 단일 (ORCH-09)
    spec = sub[0]["spec"] if sub else {}
    fields = {"task_id", "objective", "output_schema_ref", "tools_allow", "sources_hint", "boundaries", "budget", "deadline_s", "context_mode"}
    mt = o.get("metrics", {})
    check("E17-02", (fields <= set(spec), all(s["spec"]["tools_allow"] == [] for s in sub), o.get("writes"),
                     0 < mt.get("coordination_overhead_ratio", 0) < 1, mt.get("subagents") == len(sub), "overhead_alert" in mt),
          (True, True, "orchestrator-only", True, True, True))

    # E17-03 규모 결정: 단순 질문 → 단일 에이전트 모드 1개
    s = (await cl.delegate({"goal": "prompt cache 가 비용을 줄이는 원리를 설명"}, agent=orch, skill="ma.orchestrate", timeout=60)).get("output") or {}
    check("E17-03", (s.get("complexity"), s.get("mode"), len(s.get("subtasks", []))), ("simple", "single", 1))

    # E17-04 fan-in 정족수(first): 하나가 끝나면 나머지 취소 → 부분 결과로 병합
    f = (await cl.delegate({"goal": goal, "inputs": {"diff": DIFF, "data": CSV}, "join": "first"}, agent=orch, skill="ma.orchestrate", timeout=120)).get("output") or {}
    states = sorted(x.get("state") for x in f.get("subtasks", []))
    check("E17-04", (f.get("join"), states.count("TASK_STATE_COMPLETED") >= 1, f.get("partial") or states.count("TASK_STATE_COMPLETED") == len(states)),
          ("first", True, True))

    # E17-05 마감 배리어: 분석 코드가 마감보다 오래 걸리면 그 과업만 실패·부분 결과
    slow = "import time\ntime.sleep(30)\n"
    d = (await cl.delegate({"goal": goal, "inputs": {"diff": DIFF, "data": CSV, "code": slow}, "deadline_s": 4}, agent=orch, skill="ma.orchestrate", timeout=120)).get("output") or {}
    ana = next((x for x in d.get("subtasks", []) if x["spec"]["kind"] == "analysis"), {})
    check("E17-05", (d.get("partial"), ana.get("state") in ("TASK_STATE_CANCELED", "TASK_STATE_FAILED"), any(g.get("task_id") for g in (d.get("merged") or {}).get("gaps", []))),
          (True, True, True))

    # E17-06 서브에이전트 읽기 전용 강제: 쓰기 도구 위임 명세 거부
    w = await cl.delegate({"diff": DIFF, "spec": {"tools_allow": ["repo.write_file"]}}, agent=ags["ma-cr"], skill="ma.code_review", timeout=30)
    check("E17-06", (w["state"], "읽기 전용" in w.get("message", "")), ("TASK_STATE_FAILED", True))

    # E17-07 중첩 깊이: depth=1 로 들어온 오케스트레이터는 위임 불가(max_depth 2, 다음 위임이 2)
    deep = await cl.delegate({"goal": "x 조사"}, agent=orch, skill="ma.orchestrate", depth=1, timeout=30)
    check("E17-07", (deep["state"], "max_depth" in deep.get("message", "")), ("TASK_STATE_FAILED", True))

    # E17-08 조사: 출처 필수 (RES-02) · 읽기 전용
    res = (await cl.delegate({"query": "다중 에이전트 조정 비용 병목"}, agent=ags["ma-res"], skill="ma.research")).get("output") or {}
    ids = {x["id"] for x in res.get("sources", [])}
    check("E17-08", (bool(res.get("findings")), all(set(x["source_ids"]) <= ids and x["source_ids"] for x in res.get("findings", [])), res.get("read_only")),
          (True, True, True))

    # E17-09 코드 리뷰: 구조화 이슈 + SARIF 2.1.0
    cr = (await cl.delegate({"diff": DIFF}, agent=ags["ma-cr"], skill="ma.code_review")).get("output") or {}
    check("E17-09", ({i["rule"] for i in cr.get("issues", [])} >= {"hardcoded-secret", "sql-concat"}, cr.get("sarif", {}).get("version"),
                     cr.get("issues", [{}])[0].get("severity")), (True, "2.1.0", "critical"))

    # E17-10 분석 샌드박스 (ANL-01·03·05): 표·차트, 외부 네트워크 차단, 메모리·시간 한도
    an = (await cl.delegate({"data": CSV}, agent=ags["ma-anl"], skill="ma.analysis", timeout=60)).get("output") or {}
    net = (await cl.delegate({"data": CSV, "code": "import socket\nsocket.create_connection(('1.1.1.1', 80), 3)\n"}, agent=ags["ma-anl"], skill="ma.analysis", timeout=60)).get("output") or {}
    mem = (await cl.delegate({"data": CSV, "code": "x = bytearray(1500 << 20)\n"}, agent=ags["ma-anl"], skill="ma.analysis", timeout=60)).get("output") or {}
    tmo = await cl.delegate({"data": CSV, "code": "while True: pass\n", "spec": {"deadline_s": 3}}, agent=ags["ma-anl"], skill="ma.analysis", timeout=60)
    chart = (an.get("charts") or [{}])[0].get("spec", {})
    check("E17-10", (chart.get("mark"), len(an.get("tables", [{}])[0].get("rows", [])), net.get("sandbox", {}).get("exit_code") not in (0, None),
                     mem.get("sandbox", {}).get("exit_code") not in (0, None), tmo["state"] == "TASK_STATE_FAILED" and "시간 한도" in tmo.get("message", "")),
          ("bar", 3, True, True, True))

    # E17-11 구조적 병합: 같은 이슈 중복 제거·최고 심각도
    m = (await cl.delegate({"results": [{"taskId": "a", "state": "TASK_STATE_COMPLETED", "output": cr}, {"taskId": "b", "state": "TASK_STATE_COMPLETED", "output": cr}]},
                           agent=ags["ma-merge"], skill="ma.merge")).get("output") or {}
    check("E17-11", (m.get("strategy"), m.get("deduplicated"), len(m.get("issues", [])) == len(cr.get("issues", []))), ("structural", len(cr.get("issues", [])), True))

    # E17-12 skill 보안 요구 (SRV-05): 오케스트레이터 직접 호출(토큰 없음) 거부
    direct = httpx.post("http://127.0.0.1:8010/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                        "params": {"message": {"parts": [{"data": {"goal": "x"}}]}, "metadata": {"skill": "ma.orchestrate"}}}).json()
    check("E17-12", (direct.get("error") or {}).get("code"), -32051)


asyncio.run(main())
sys.exit(1 if failed else 0)
