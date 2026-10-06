"""MA-ANL Subagent C · Analysis — 데이터를 격리 샌드박스에서 코드로 계산해 수치·표·차트(vega-lite)·해석을 돌려준다 (FR-MA-ANL-01·03·05)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from atlas_agent_core import AgentSpec, InputRequired, Skill, TaskContext, secrets
from units.ma_common import check_spec, estimate_tokens

MANIFEST = Path(__file__).with_name("atlas-agent.json")

# 코드가 없을 때 쓰는 기본 분석: CSV 를 읽어 수치 열 요약·첫 범주 열 기준 집계·막대 차트 명세
DEFAULT_CODE = r'''
import json, pandas as pd
df = pd.read_csv("data.csv")
num = df.select_dtypes("number")
metrics = [{"name": f"{c}.mean", "value": round(float(num[c].mean()), 4), "unit": ""} for c in num.columns]
metrics += [{"name": "rows", "value": int(len(df)), "unit": "count"}]
cat = next((c for c in df.columns if c not in num.columns), None)
charts, table = [], df.head(50)
if cat is not None and len(num.columns):
    g = df.groupby(cat)[num.columns[0]].sum().reset_index()
    table = g
    charts.append({"spec": {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "mark": "bar",
                            "data": {"values": g.to_dict("records")},
                            "encoding": {"x": {"field": cat, "type": "nominal"}, "y": {"field": num.columns[0], "type": "quantitative"}}}})
json.dump({"metrics": metrics, "tables": [{"columns": list(table.columns), "rows": table.head(50).values.tolist()}], "charts": charts,
           "interpretation": f"{len(df)}행 · 수치 열 {list(num.columns)}" + (f" · {cat} 별 {num.columns[0]} 합계" if charts else "")},
          open("result.json", "w"), ensure_ascii=False, default=str)
'''


async def analysis(ctx: TaskContext) -> dict[str, Any]:
    spec = check_spec(ctx.data)
    inputs = ctx.data.get("inputs") or {}
    data = ctx.data.get("data") or inputs.get("data")
    if not data:
        raise InputRequired("분석할 데이터(data: CSV 문자열)가 필요합니다")
    cfg = agent.manifest["spec"]["config"]
    code = ctx.data.get("code") or inputs.get("code") or DEFAULT_CODE
    # 위임 마감이 있으면 샌드박스 한도를 1초 짧게 — 오케스트레이터가 취소하기 전에 실패를 보고한다
    limit = max(1, int(float(spec["deadline_s"])) - 1) if spec.get("deadline_s") else int(cfg["timeoutS"])
    body = {"code": code, "files": {"data.csv": data}, "timeout_s": limit, "mem_mb": cfg["memMb"]}
    url = os.environ.get("ATLAS_SANDBOX_URL", "http://sandbox:8000").rstrip("/") + "/run"
    async with httpx.AsyncClient(timeout=body["timeout_s"] + 15) as c:
        for fresh in (False, True):   # 401 = 회전된 토큰 → 중개에서 새 값을 받아 한 번 더
            token = await secrets.get("sandbox-token", env="ATLAS_SANDBOX_TOKEN", dev_default="local-dev-sandbox", fresh=fresh)
            res = await c.post(url, json=body, headers={"X-Sandbox-Token": token})
            if res.status_code != 401:
                break
    res.raise_for_status()
    run = res.json()
    r = run.get("result") or {}
    caveats = []
    if run.get("timed_out"):   # 자원 한도 초과는 분석 실패 (FR-MA-ANL-05) — 부분 결과로 오인하지 않게
        raise TimeoutError(f"샌드박스 시간 한도 {run['limits']['timeout_s']}s 초과로 중단")
    if run.get("exit_code") not in (0, None):
        caveats.append(f"코드 종료 코드 {run.get('exit_code')}: {run.get('stderr', '')[-300:]}")
    tables = [{"columns": t.get("columns", []), "rows": (t.get("rows") or [])[: int(cfg["maxRows"])]} for t in r.get("tables", [])]
    return {"kind": "analysis", "metrics": r.get("metrics", []), "tables": tables, "charts": r.get("charts", []),
            "interpretation": r.get("interpretation", ""), "caveats": caveats, "sandbox": {k: run.get(k) for k in ("exit_code", "timed_out", "duration_ms", "limits")},
            "stdout": run.get("stdout", "")[:2000], "read_only": True, "usage": {"tokens": estimate_tokens(json.dumps(r, default=str) + code)}}


agent = AgentSpec.from_file(MANIFEST, description="MA-ANL 분석 서브에이전트 — 샌드박스 코드 실행(자원 한도), AnalysisResult{metrics, tables, charts(vega-lite), interpretation, caveats}",
                            skills=[Skill(id="ma.analysis", name="Analysis", handler=analysis,
                                          description="데이터(CSV) + 질문 → 수치·표·차트·해석 analysis 분석 데이터 통계 차트 table chart",
                                          tags=["analysis", "분석", "data"], examples=["지역별 매출 합계를 비교"])])
