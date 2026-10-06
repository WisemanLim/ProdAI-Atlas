"""atlas eval CLI (FR-EV-REG-06) — exit 0 통과 · 100 게이트 실패 · 2 실행 오류 (promptfoo 관례).

  python -m units.ev.cli lint  --suite evals/planner-smoke.yaml [--server URL]
  python -m units.ev.cli run   --suite evals/planner-smoke.yaml [--server URL] [--ci]
  python -m units.ev.cli seed  --dataset planner-golden --file evals/planner-golden.jsonl [--approve] [--snapshot]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

PASS, GATE_FAIL, ERROR = 0, 100, 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas eval")
    ap.add_argument("cmd", choices=["lint", "run", "seed"])
    ap.add_argument("--suite")
    ap.add_argument("--server", default=os.environ.get("ATLAS_EV_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--ci", action="store_true", help="요약 한 줄 + 실패 사유, exit code 로 게이트")
    ap.add_argument("--dataset")
    ap.add_argument("--file")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--input-schema", help="seed: 새 데이터셋의 입력 JSON Schema (JSON 문자열)")
    a = ap.parse_args(argv)
    try:
        with httpx.Client(base_url=a.server, timeout=600) as c:
            if a.cmd == "seed":
                return seed(c, a)
            text = Path(a.suite).read_text(encoding="utf-8")
            if a.cmd == "lint":
                r = c.post("/v1/suites/lint", content=text)
                print(json.dumps(r.json(), ensure_ascii=False))
                return PASS if r.status_code == 200 else ERROR
            r = c.post("/v1/runs", content=text)
            if r.status_code != 200:
                print(f"ERROR {r.status_code}: {r.text[:300]}", file=sys.stderr)
                return ERROR
            out = r.json()
    except (httpx.HTTPError, OSError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return ERROR
    s = out["summary"]
    line = (f"{out['suite']} run={out['run_id']} dataset={out['dataset']}@v{out['dataset_version']} gate={out['gate'].upper()} "
            f"pass_rate={s['pass_rate']} pass^k={s['pass_hat_k']} errors={s['errors']} cost=${s['cost_usd']}")
    print(line)
    if not a.ci:
        print(json.dumps(s["per_target"], ensure_ascii=False, indent=2))
    for reason in s.get("gate_reasons", []):
        print(f"  - {reason}")
    return PASS if out["gate"] == "pass" else GATE_FAIL


def seed(c: httpx.Client, a) -> int:
    if any(d["name"] == a.dataset for d in c.get("/v1/datasets").json()["items"]):
        print(f"dataset {a.dataset} 존재 — 항목만 추가")
        if a.input_schema:
            r = c.patch(f"/v1/datasets/{a.dataset}", json={"input_schema": json.loads(a.input_schema)})
            print(f"input_schema: {r.status_code} {r.text[:200] if r.status_code != 200 else 'ok'}")
    else:
        schema = json.loads(a.input_schema) if a.input_schema else None
        c.post("/v1/datasets", json={"name": a.dataset, "description": f"seed from {a.file}", "input_schema": schema}).raise_for_status()
    r = c.post(f"/v1/datasets/{a.dataset}/items:bulk", content=Path(a.file).read_bytes())
    r.raise_for_status()
    print(f"bulk: {r.json()}")
    if a.approve:
        token = {"authorization": f"Bearer {os.environ.get('ATLAS_EV_CURATOR_TOKEN', '')}"}
        for it in c.get(f"/v1/datasets/{a.dataset}/items", params={"status": "draft"}).json()["items"]:
            c.post(f"/v1/items/{it['id']}/transition", json={"to": "in_review"}).raise_for_status()
            c.post(f"/v1/items/{it['id']}/transition", json={"to": "approved", "verdict": "pass"}, headers=token).raise_for_status()
    if a.snapshot:
        print(f"snapshot: {c.post(f'/v1/datasets/{a.dataset}/versions').json()}")
    return PASS


if __name__ == "__main__":
    sys.exit(main())
