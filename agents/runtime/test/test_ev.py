"""Evals — 골든셋 CRUD·검수·불변 버전·trace 승격 · 스위트 lint·채점기·타깃 3종·pass^k·게이트·CLI exit code."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas_agent_core import create_app
from units import ev
from units.ev import cli, runner, scorers
from units.ev import suite as suite_mod

CURATOR = {"authorization": "Bearer test-curator"}


@pytest.fixture(scope="module")
def evc(client):
    import os

    from app import db
    os.environ["ATLAS_EV_CURATOR_TOKEN"] = "test-curator"
    with TestClient(create_app(ev.agent, engine=db.engine, public_url="http://ev:8000")) as c:
        yield c


def approve(c, iid):
    c.post(f"/v1/items/{iid}/transition", json={"to": "in_review"})
    return c.post(f"/v1/items/{iid}/transition", json={"to": "approved", "verdict": "pass", "critique": "ok"}, headers=CURATOR)


def test_dataset_items_review_and_versions(evc):
    schema = {"type": "object", "required": ["goal"], "properties": {"goal": {"type": "string"}}}
    assert evc.post("/v1/datasets", json={"name": "ds-a", "input_schema": schema}).status_code == 201
    assert evc.post("/v1/datasets", json={"name": "ds-a"}).status_code == 409
    assert evc.post("/v1/datasets/ds-a/items", json={"input": {"nope": 1}}).status_code == 422
    it = evc.post("/v1/datasets/ds-a/items", json={"input": {"goal": "a"}, "expected_output": {"steps": 1}}).json()
    assert it["status"] == "draft"
    assert evc.post(f"/v1/items/{it['id']}/transition", json={"to": "approved"}, headers=CURATOR).status_code == 409  # draft→approved 금지
    evc.post(f"/v1/items/{it['id']}/transition", json={"to": "in_review"})
    assert evc.post(f"/v1/items/{it['id']}/transition", json={"to": "approved"}).status_code == 403     # 큐레이터 아님
    done = evc.post(f"/v1/items/{it['id']}/transition", json={"to": "approved", "verdict": "pass"}, headers=CURATOR).json()
    assert done["status"] == "approved" and [lb["to"] for lb in done["labels"]] == ["in_review", "approved"] and done["labels"][1]["actor"] == "curator"
    assert evc.patch(f"/v1/items/{it['id']}", json={"input": {"goal": "b"}}).status_code == 409           # 승인 항목 불변
    v1 = evc.post("/v1/datasets/ds-a/versions").json()
    it2 = evc.post("/v1/datasets/ds-a/items", json={"input": {"goal": "c"}}).json()
    approve(evc, it2["id"])
    v2 = evc.post("/v1/datasets/ds-a/versions").json()
    frozen1 = evc.get("/v1/datasets/ds-a/versions/1").json()
    assert (v1["version"], v1["items"], v2["version"], v2["items"]) == (1, 1, 2, 2) and v1["manifest_sha256"] != v2["manifest_sha256"]
    assert [i["input"] for i in frozen1["items"]] == [{"goal": "a"}]                                       # 이후 변경과 무관


def test_patch_schema_rejects_nonconforming_active_items(evc):
    evc.post("/v1/datasets", json={"name": "ds-schema"})
    bad = evc.post("/v1/datasets/ds-schema/items", json={"input": {"prompt": "x"}}).json()
    schema = {"input_schema": {"type": "object", "required": ["goal"]}}
    assert evc.patch("/v1/datasets/ds-schema", json=schema).status_code == 409
    evc.post(f"/v1/items/{bad['id']}/transition", json={"to": "retired"})
    assert evc.patch("/v1/datasets/ds-schema", json=schema).status_code == 200


def test_bulk_jsonl_reports_bad_lines(evc):
    evc.post("/v1/datasets", json={"name": "ds-bulk", "input_schema": {"type": "object", "required": ["goal"]}})
    body = '{"input": {"goal": "x"}}\n\nnot json\n{"input": {"other": 1}}\n{"input": {"goal": "y"}, "tags": ["t"]}'
    r = evc.post("/v1/datasets/ds-bulk/items:bulk", content=body).json()
    assert r["accepted"] == 2 and [x["line"] for x in r["rejected"]] == [3, 4]


def test_promote_trace_to_review_queue(evc, monkeypatch):
    monkeypatch.setenv("ATLAS_GUARD_URL", "http://gr")

    def handler(req: httpx.Request):
        if req.url.path.endswith("/io"):
            if "missing" in req.url.path:
                return httpx.Response(404)
            return httpx.Response(200, json={"input": "고객 010-1234-5678 주문 취소", "output": "취소됨", "captured": True})
        body = json.loads(req.content)
        return httpx.Response(200, json={"decision": "transform", "contents": [t.replace("010-1234-5678", "***-****-5678") for t in body["contents"]]})
    monkeypatch.setattr(ev, "TRANSPORT", httpx.MockTransport(handler))
    evc.post("/v1/datasets", json={"name": "ds-trace"})
    it = evc.post("/v1/datasets/ds-trace/items:from-trace", json={"trace_id": "t-1"}).json()
    assert it["status"] == "in_review" and it["source"] == "prod-trace" and it["source_trace_id"] == "t-1"
    assert it["input"] == {"prompt": "고객 ***-****-5678 주문 취소"} and it["expected_output"] == {"observed_output": "취소됨"}
    assert evc.post("/v1/datasets/ds-trace/items:from-trace", json={"trace_id": "missing"}).status_code == 404
    evc.post("/v1/datasets", json={"name": "ds-goal-only", "input_schema": {"type": "object", "required": ["goal"]}})
    assert evc.post("/v1/datasets/ds-goal-only/items:from-trace", json={"trace_id": "t-2"}).status_code == 422   # 형식 혼입 차단


@pytest.mark.parametrize("bad,msg", [
    ({"name": "s", "dataset": {"name": "d"}, "targets": [{"type": "prompt"}], "scorers": [{"type": "contains", "value": "x"}]}, "model"),
    ({"name": "s", "dataset": {"name": "d"}, "targets": [{"type": "http", "url": "u"}], "scorers": [{"type": "nope"}]}, "scorers"),
    ({"name": "s", "dataset": {"name": "d"}, "targets": [{"type": "http", "url": "u"}], "scorers": [{"type": "contains"}]}, "value"),
    ({"name": "s", "dataset": {"name": "d"}, "targets": [{"type": "http", "url": "u"}], "scorers": [{"type": "regex", "pattern": "("}]}, "subpattern"),
])
def test_suite_lint(bad, msg):
    with pytest.raises(Exception, match=msg):
        suite_mod.load(bad)


def test_render_scorers_and_pass_hat_k():
    item = {"input": {"goal": "g", "n": 2}, "expected_output": {"steps": 2}}
    assert suite_mod.render("목표: {{input.goal}}", item) == "목표: g" and suite_mod.render({"x": "{{input.n}}"}, item) == {"x": 2}
    out = '{"steps": [1, 2], "dod": [1]}'
    assert scorers.score({"type": "json_schema", "schema": {"type": "object", "required": ["dod"]}}, out, item, 1, 0)["passed"]
    assert scorers.score({"type": "equals", "path": "steps.1", "value": 2}, out, item, 1, 0)["passed"]
    assert not scorers.score({"type": "not_contains", "value": "steps"}, out, item, 1, 0)["passed"]
    assert scorers.score({"type": "latency_lt_ms", "value": 10}, out, item, 5, 0)["passed"]
    assert runner.pass_hat_k(3, 2, 3) == 0 and runner.pass_hat_k(5, 4, 2) == 0.6 and runner.pass_hat_k(3, 3, 3) == 1


def target_transport(flaky_every=None):
    calls = {"n": 0}

    def handler(req: httpx.Request):
        calls["n"] += 1
        body = json.loads(req.content)
        if req.url.path == "/v1/chat/completions":
            goal = body["messages"][-1]["content"]
            steps = [{"title": p} for p in goal.split(". ") if p]
            if flaky_every and calls["n"] % flaky_every == 0:
                return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}, headers={"x-atlas-cost-usd": "0.00000100"})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"steps": steps, "dod": [{"type": "t"}]})}}]},
                                  headers={"x-atlas-cost-usd": "0.00000100"})
        if req.url.path == "/a2a":
            data = body["params"]["message"]["parts"][0]["data"]
            plan = {"steps": [{"title": data["goal"]}], "dod": [{"type": "human"}]}
            return httpx.Response(200, json={"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [{"data": plan}]}]}}})
        return httpx.Response(200, text=json.dumps({"echo": body}))
    return httpx.MockTransport(handler)


SUITE = """
name: unit-suite
dataset: {name: ds-run}
targets:
  - {id: p, type: prompt, model: planner-default, template: "{{input.goal}}", response_format: json_object}
  - {id: a, type: agent, url: http://al-plan, skill: loop.plan, data_template: {goal: "{{input.goal}}"}}
scorers:
  - {type: json_schema, schema: {type: object, required: [steps, dod]}}
epochs: 3
thresholds: {pass_rate: 1.0, pass_hat_k: 1.0}
"""


def test_run_suite_gate_pass_and_fail(evc, monkeypatch):
    evc.post("/v1/datasets", json={"name": "ds-run"})
    for g in ("A. B", "C"):
        approve(evc, evc.post("/v1/datasets/ds-run/items", json={"input": {"goal": g}}).json()["id"])
    evc.post("/v1/datasets/ds-run/versions")
    ok = asyncio.run(ev.run_suite(SUITE, transport=target_transport()))
    s = ok["summary"]
    assert ok["gate"] == "pass" and s["results"] == 12 and s["pass_rate"] == 1.0 and s["per_target"]["p"]["cost_usd"] == 0.000006
    flaky = asyncio.run(ev.run_suite(SUITE, transport=target_transport(flaky_every=4)))
    assert flaky["gate"] == "fail" and flaky["summary"]["pass_hat_k"] < 1.0 and any("pass" in r for r in flaky["summary"]["gate_reasons"])
    stored = evc.get(f"/v1/runs/{flaky['run_id']}", params={"results": True}).json()
    assert {r["run_id"] for r in evc.get("/v1/runs").json()["items"]} >= {ok["run_id"], flaky["run_id"]}
    assert stored["gate"] == "fail" and len(stored["results"]) == 12
    drop = SUITE.replace("thresholds: {pass_rate: 1.0, pass_hat_k: 1.0}", f"thresholds: {{pass_rate: 0.0, max_drop_pp: 2}}\nbaseline_run_id: {ok['run_id']}")
    regressed = asyncio.run(ev.run_suite(drop, transport=target_transport(flaky_every=4)))
    assert regressed["gate"] == "fail" and "기준선" in regressed["summary"]["gate_reasons"][0]


def test_cli_exit_codes(evc, monkeypatch, tmp_path, capsys):
    path = tmp_path / "s.yaml"
    path.write_text(SUITE)
    monkeypatch.setattr(ev, "TRANSPORT", target_transport())

    class Bridge(httpx.Client):          # CLI → TestClient(앱) 연결
        def __init__(self, base_url, timeout):
            pass

        def __enter__(self):
            return evc

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(cli.httpx, "Client", Bridge)
    assert cli.main(["run", "--suite", str(path), "--ci"]) == cli.PASS
    assert "gate=PASS" in capsys.readouterr().out
    monkeypatch.setattr(ev, "TRANSPORT", target_transport(flaky_every=4))
    assert cli.main(["run", "--suite", str(path), "--ci"]) == cli.GATE_FAIL
    path.write_text(SUITE.replace("ds-run", "no-such-dataset"))
    assert cli.main(["run", "--suite", str(path), "--ci"]) == cli.ERROR
