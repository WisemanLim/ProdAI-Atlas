"""BL — 채택 규칙·후보 평가·카나리(고정 배정 통계·SLO 롤백)·별칭 전환·EOL·모델 카드·프롬프트/하네스 게시 게이트·플래그·ablation·best-of-n·곡선."""
import copy
import json
import re
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas_agent_core import create_app, events, flags, prompts
from units import bl
from units.bl import clients, expr, models


class FakeGateway:
    def __init__(self):
        self.cfg = {"chat-default": [{"provider": "mock", "model": "mock-chat", "weight": 1}],
                    "chat-large": [{"provider": "mock", "model": "mock-chat-large", "weight": 1}]}
        self.ov, self.hist, self.stats = {}, {}, {}
        self.catalog = [
            {"model": "mock-chat", "provider": "mock", "context_window": 128000, "modalities": ["text"], "tools": False, "prices": {"input": "0.25", "output": "1.00"}},
            {"model": "mock-chat-small", "provider": "mock", "context_window": 32000, "modalities": ["text"], "tools": False, "prices": {"input": "0.05", "output": "0.20"}},
            {"model": "mock-chat-large", "provider": "mock", "context_window": 200000, "modalities": ["text", "image"], "tools": True,
             "prices": {"input": "3.00", "output": "15.00"}, "deprecation_date": "2026-11-15", "deprecation_in": 44},
            {"model": "mock-reasoner", "provider": "mock", "context_window": 200000, "modalities": ["text"], "tools": True, "prices": {"input": "2.00", "output": "8.00"}}]

    def current(self, name):
        if name in self.ov:
            return self.ov[name]
        if name in self.cfg:
            return {"name": name, "deployments": copy.deepcopy(self.cfg[name]), "fallbacks": [], "version": 0, "source": "config"}
        return None

    def model_of(self, name):
        a = self.current(name)
        return max(a["deployments"], key=lambda d: d["weight"])["model"]

    def handle(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else {}
        if path == "/admin/econ/models":
            return httpx.Response(200, json={"models": self.catalog})
        if path == "/admin/aliases":
            names = set(self.cfg) | set(self.ov)
            return httpx.Response(200, json={"data": [self.current(n) for n in sorted(names)]})
        g = re.match(r"^/admin/aliases/([^/]+)(/history|/rollback|/stats)?$", path)
        if g:
            name, sub = g.group(1), g.group(2)
            if sub == "/history":
                return httpx.Response(200, json={"data": self.hist.get(name, [])})
            if sub == "/stats":
                cur = self.current(name)
                v = int(req.url.params.get("version", cur["version"]))
                return httpx.Response(200, json={"models": [self.stats.get((name, v, d["model"]), {"model": d["model"], "requests": 0}) for d in cur["deployments"]]})
            if sub == "/rollback":
                h = next(x for x in self.hist[name] if x["version"] == body["version"])
                return self.put(name, h)
            if m == "GET":
                cur = self.current(name)
                return httpx.Response(200, json=cur) if cur else httpx.Response(404, json={"error": {"message": "no alias"}})
            if m == "PUT":
                return self.put(name, body)
            if m == "DELETE":
                if name in self.ov:
                    self.hist.setdefault(name, []).insert(0, self.ov.pop(name))
                return httpx.Response(204)
        if path == "/v1/chat/completions":
            last = body["messages"][-1]["content"]
            s = f" [s={(body.get('seed', 0) * 3 + 1) % 10}]" if body.get("temperature", 0) > 0 and "seed" in body else ""
            return httpx.Response(200, json={"choices": [{"message": {"content": f"[mock] {last}{s}"}}]}, headers={"x-atlas-cost-usd": "0.001"})
        return httpx.Response(404, json={"error": {"message": f"fake gw: {m} {path}"}})

    def put(self, name, body):
        prev = self.current(name)
        if prev:
            self.hist.setdefault(name, []).insert(0, copy.deepcopy(prev))
        e = {"name": name, "deployments": body["deployments"], "fallbacks": body.get("fallbacks") or [], "version": (prev or {}).get("version", 0) + 1,
             "note": body.get("note", ""), "source": "overlay"}
        self.ov[name] = e
        return httpx.Response(200, json=e)


class FakeEV:
    QUALITY: ClassVar[dict[str, float]] = {"mock-chat": 0.80, "mock-chat-small": 0.80, "mock-reasoner": 0.95, "mock-chat-large": 0.90}
    COST: ClassVar[dict[str, float]] = {"mock-chat": 0.0010, "mock-chat-small": 0.0002, "mock-reasoner": 0.0080, "mock-chat-large": 0.0150}
    EFFORT_COST: ClassVar[dict[str, float]] = {"low": 0.001, "medium": 0.002, "high": 0.004}

    def __init__(self, gw):
        self.gw, self.runs, self.n, self.suites = gw, {}, 0, []

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v1/runs":
            s = json.loads(req.content)
            self.suites.append(s)
            self.n += 1
            rid = f"run-{self.n}"
            t = s["targets"][0]
            md = t.get("metadata") or {}
            if t["type"] == "prompt":
                model = self.gw.model_of(t["model"]) if self.gw.current(t["model"]) else t["model"]
                q, cost = self.QUALITY.get(model, 0.5), self.COST.get(model, 0.001)
                if t.get("effort"):
                    q, cost = 0.9, self.EFFORT_COST[t["effort"]]
            else:   # 에이전트 타깃: 보정 플래그·고정 버전에 따라 품질이 달라진다
                fl = md.get("atlas.flags", {})
                q = 0.0 if fl.get("harness.plan_json_repair.enabled") is False else 1.0
                if (md.get("atlas.prompts") or {}).get("al-plan.system") == 2:
                    q = 0.2           # 프롬프트 v2 는 퇴행
                cost = 0.001
            per = {t["id"]: {"pass_hat_k": q, "pass_rate": q, "cost_usd": cost * 10, "results": 10, "p95_latency_ms": 100, "errors": 0}}
            summ = {"pass_rate": q, "pass_hat_k": q, "cost_usd": cost * 10, "errors": 0, "per_target": per, "gate_reasons": []}
            gate = "pass" if q >= s.get("thresholds", {}).get("pass_rate", 1.0) else "fail"
            self.runs[rid] = summ
            return httpx.Response(200, json={"run_id": rid, "gate": gate, "summary": summ})
        g = re.match(r"^/v1/runs/([^/]+)/diff/([^/]+)$", path)
        if g:
            a, b = self.runs[g.group(1)]["pass_rate"], self.runs[g.group(2)]["pass_rate"]
            delta = round(b - a, 4)
            return httpx.Response(200, json={"matrix": {"pass_pass": 0, "pass_fail": 10 if delta < -0.5 else 0, "fail_pass": 0, "fail_fail": 0},
                                             "delta_pass_rate": delta, "ci95": [delta, delta], "mcnemar_p": 0.01 if abs(delta) >= 0.1 else 0.6,
                                             "significant": abs(delta) >= 0.1})
        return httpx.Response(404)


@pytest.fixture
def fx(client, tmp_path, monkeypatch):
    from app import db
    gw = FakeGateway()
    evx = FakeEV(gw)
    monkeypatch.setenv("ATLAS_GATEWAY_ADMIN_TOKEN", "test-gw-admin")
    monkeypatch.setenv("ATLAS_FLAGS_FILE", str(tmp_path / "flags.json"))
    monkeypatch.setattr(clients, "TRANSPORT", httpx.MockTransport(lambda r: evx.handle(r) if r.url.host == "agent-ev" else gw.handle(r)))
    events.sent.clear()
    with TestClient(create_app(bl.agent, engine=db.engine, public_url="http://bl:8000")) as c:
        yield c, gw, evx


SUITE = {"name": "chat-qa", "dataset": {"name": "qa"}, "scorers": [{"type": "contains", "value": "x"}],
         "targets": [{"id": "prod", "type": "prompt", "model": "chat-default", "template": "{{input.q}}"}]}


def subjects():
    return [s for s, _ in events.sent]


def test_expr_is_safe_and_decides():
    assert expr.evaluate("a >= b - 0.01 and max(c, 2) == 3", {"a": 1, "b": 1, "c": 3}) is True
    for bad in ("__import__('os')", "a.__class__", "(lambda: 1)()", "[x for x in a]", "open('f')"):
        with pytest.raises(expr.ExprError):
            expr.evaluate(bad, {"a": [1]})
    m = {"base_pass_hat_k": 0.8, "cand_pass_hat_k": 0.95, "base_cost_per_case": 0.001, "cand_cost_per_case": 0.008, "base_p95_ms": 100, "cand_p95_ms": 120}
    assert models.decide(models.DEFAULT_RULES, m, significant=True)[0] == "review-cost"
    assert models.decide(models.DEFAULT_RULES, m, significant=False)[0] == "insufficient-evidence"
    assert models.decide(models.DEFAULT_RULES, {**m, "cand_pass_hat_k": 0.5}, True)[0] == "hold"
    assert models.decide(models.DEFAULT_RULES, {**m, "cand_cost_per_case": 0.001}, True)[0] == "promote"


def test_candidate_evaluate_canary_promote_and_card(fx):
    c, gw, evx = fx
    r = c.post("/v1/models/mock-chat-small/candidate", json={"alias": "chat-default", "suites": [SUITE], "k": 4})
    assert r.status_code == 201, r.text
    cand = r.json()
    assert (cand["status"], cand["decision"]) == ("decided", "promote")        # 같은 품질 · 1/5 비용
    assert {d["model"]: d["weight"] for d in gw.ov["chat-default"]["deployments"]} == {"mock-chat": 1, "mock-chat-small": 0}
    assert [s["epochs"] for s in evx.suites] == [4, 4] and not any(k.startswith("bl-") for k in gw.ov)   # 임시 alias 정리
    assert {"plt.bl.model.candidate_added", "plt.bl.model.decided"} <= set(subjects())
    cid = cand["id"]
    assert c.post(f"/v1/candidates/{cid}/canary", json={"stages": [10, 0]}).status_code == 422
    st = c.post(f"/v1/candidates/{cid}/canary", json={"stages": [10, 50, 100], "min_samples": 5}).json()
    v = st["detail"]["canary"]["alias_version"]
    assert {d["model"]: d["weight"] for d in gw.ov["chat-default"]["deployments"]} == {"mock-chat": 90, "mock-chat-small": 10}
    assert c.post(f"/v1/candidates/{cid}/canary/advance").json()["action"] == "wait"
    ok = {"requests": 20, "errors": 0, "error_rate": 0.0, "p95_ms": 100, "cost_per_request_usd": 0.0002}
    gw.stats[("chat-default", v, "mock-chat-small")] = {"model": "mock-chat-small", **ok}
    gw.stats[("chat-default", v, "mock-chat")] = {"model": "mock-chat", **ok, "cost_per_request_usd": 0.001}
    a1 = c.post(f"/v1/candidates/{cid}/canary/advance").json()
    assert (a1["action"], a1["pct"]) == ("advanced", 50)
    v2 = gw.ov["chat-default"]["version"]
    gw.stats[("chat-default", v2, "mock-chat-small")] = {"model": "mock-chat-small", **ok}
    a2 = c.post(f"/v1/candidates/{cid}/canary/advance").json()
    assert a2["action"] == "promoted"
    cur = gw.ov["chat-default"]
    assert [d["model"] for d in cur["deployments"]] == ["mock-chat-small"] and cur["fallbacks"] == ["chat-default-prev"]
    assert gw.model_of("chat-default-prev") == "mock-chat" and "plt.bl.model.promoted" in subjects()
    card = c.get("/v1/models/mock-chat-small/card").text
    assert "# 모델 카드 — mock-chat-small" in card and "promote" in card and "candidate.promote" in card
    # 원클릭 롤백 (후보 등록 전 = 설정 파일 alias)
    rb = c.post(f"/v1/candidates/{cid}/rollback", json={"by": "ops", "reason": "고객 불만"}).json()
    assert rb["status"] == "rolled_back" and "chat-default" not in gw.ov


def test_canary_slo_violation_rolls_back(fx):
    c, gw, _ = fx
    cid = c.post("/v1/models/mock-chat-small/candidate", json={"alias": "chat-default", "suites": [SUITE]}).json()["id"]
    v = c.post(f"/v1/candidates/{cid}/canary", json={"min_samples": 3}).json()["detail"]["canary"]["alias_version"]
    gw.stats[("chat-default", v, "mock-chat-small")] = {"model": "mock-chat-small", "requests": 10, "errors": 3, "error_rate": 0.3, "p95_ms": 90, "cost_per_request_usd": 0.0002}
    gw.stats[("chat-default", v, "mock-chat")] = {"model": "mock-chat", "requests": 100, "errors": 0, "error_rate": 0.0, "p95_ms": 90, "cost_per_request_usd": 0.001}
    out = c.post(f"/v1/candidates/{cid}/canary/advance").json()
    assert out["action"] == "rolled_back" and "error_rate" in out["reasons"][0] and "chat-default" not in gw.ov
    assert c.get(f"/v1/candidates/{cid}").json()["status"] == "rolled_back" and "plt.bl.model.rolled_back" in subjects()
    # 품질이 크게 좋은 고비용 후보 → review-cost, 카나리는 force 없이 거부
    hi = c.post("/v1/models/mock-reasoner/candidate", json={"alias": "chat-default", "suites": [SUITE]}).json()
    assert hi["decision"] == "review-cost" and c.post(f"/v1/candidates/{hi['id']}/canary", json={}).status_code == 422


def test_eol_scan_registers_replacement(fx):
    c, _, _ = fx
    e = c.get("/v1/models/eol").json()["items"]
    assert e == [{"model": "mock-chat-large", "deprecation_date": "2026-11-15", "days_left": 44, "aliases": ["chat-large"], "replacement": "mock-reasoner"}]
    out = c.post("/v1/models/eol/scan", json={"suites": [{**SUITE, "targets": [{**SUITE["targets"][0], "model": "chat-large"}]}], "evaluate": True}).json()
    assert len(out["registered"]) == 1 and out["registered"][0]["model"] == "mock-reasoner" and "plt.bl.model.eol_warning" in subjects()
    again = c.post("/v1/models/eol/scan", json={}).json()
    assert again["registered"] == []                                           # 진행 중 후보가 있으면 중복 등록 안 함


def agent_suite():
    return {"name": "plan-gate", "dataset": {"name": "plans"}, "scorers": [{"type": "contains", "value": "steps"}], "thresholds": {"pass_rate": 0.9},
            "targets": [{"id": "al-plan", "type": "agent", "url": "http://agent-al-plan:8000", "data_template": {"goal": "{{input.goal}}"}}]}


def test_prompt_registry_publish_gate_and_client(fx):
    c, _, evx = fx
    p1 = c.post("/v1/prompts/al-plan.system", json={"template": "plan {{goal}}", "variables": {"goal": "str"}}).json()
    assert (p1["version"], p1["labels"]) == (1, ["staging"])
    assert c.post("/v1/prompts/al-plan.system", json={"template": "plan {{nope}}", "variables": {"goal": "str"}}).status_code == 422
    c.post("/v1/prompts/al-plan.system", json={"template": "plan v2 {{goal}}", "variables": {"goal": "str"}})
    assert c.post("/v1/prompts/al-plan.system/labels/production", json={"version": 1}).status_code == 422           # 게이트 스위트 필요
    ok = c.post("/v1/prompts/al-plan.system/labels/production", json={"version": 1, "gate_suite": agent_suite()}).json()
    assert ok["gate"]["gate"] == "pass" and evx.suites[-1]["targets"][0]["metadata"] == {"atlas.prompts": {"al-plan.system": 1}}
    blocked = c.post("/v1/prompts/al-plan.system/labels/production", json={"version": 2, "gate_suite": agent_suite()})
    assert blocked.status_code == 409 and c.get("/v1/prompts/al-plan.system?label=production").json()["version"] == 1
    assert c.post("/v1/prompts/al-plan.system/labels/production", json={"version": 2, "override": True, "reason": "긴급"}).status_code == 403
    ov = c.post("/v1/prompts/al-plan.system/labels/production", json={"version": 2, "override": True, "reason": "긴급 핫픽스"},
                headers={"authorization": "Bearer test-gw-admin"})
    assert ov.status_code == 200 and any(a["action"] == "prompt.publish.override" for a in c.get("/v1/audit").json()["items"])
    assert "plt.bl.prompt.published" in subjects()
    # SDK 클라이언트: 라벨 조회 · 버전 고정 · BL 없음 → fallback
    import asyncio
    import os
    prompts.transport = httpx.ASGITransport(app=c.app)
    os.environ["ATLAS_BL_URL"] = "http://bl"
    try:
        prompts.clear_cache()
        p = asyncio.run(prompts.get("al-plan.system"))
        tok = prompts.pin({"al-plan.system": 1})
        pinned = asyncio.run(prompts.get("al-plan.system"))
        prompts.unpin(tok)
        assert (p.version, p.render(goal="x"), pinned.version) == (2, "plan v2 x", 1)
    finally:
        prompts.transport = None
        os.environ.pop("ATLAS_BL_URL")
        prompts.clear_cache()
    assert asyncio.run(prompts.get("al-plan.system", fallback="builtin")).template == "builtin"


def test_harness_rules_flags_ablation(fx, tmp_path):
    c, _, _ = fx
    h = c.post("/v1/harness/al-run", json={"manifest": "loop:\n  max_turns: 7\ntools: [atlas_calc]\n"}).json()
    assert h["manifest"] == {"loop": {"max_turns": 7}, "tools": ["atlas_calc"]}
    assert c.post("/v1/harness/al-run", json={"manifest": {"loop": {"max_turns": 999}}}).status_code == 422
    rs = c.post("/v1/rules/refund", json={"yaml": "rules:\n- {id: big, when: 'amount > 100000', action: require_approval}\n- {id: ban, when: 'country in [\"KP\"]', action: deny}\n"})
    assert rs.status_code == 201
    assert c.post("/v1/rules/refund", json={"yaml": "rules:\n- {id: x, when: 'a.b > 1'}\n"}).status_code == 422
    ev1 = c.post("/v1/rules/refund/evaluate?label=staging", json={"facts": {"amount": 200000, "country": "KR"}}).json()
    assert ev1["decision"] == "require_approval" and [m["id"] for m in ev1["matched"]] == ["big"]
    assert c.post("/v1/rules/refund/evaluate?label=staging", json={"facts": {"amount": 1, "country": "KP"}}).json()["decision"] == "deny"
    skill = c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"metadata": {"skill": "bl.validate_rule"},
                   "message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"data": {"rules": "refund", "facts": {"amount": 5, "country": "KR"}}}]}}}).json()
    assert skill["result"]["task"]["artifacts"][0]["parts"][0]["data"]["decision"] == "allow"
    # 플래그: 메타데이터 필수 · flagd 파일 형식
    assert c.put("/v1/flags/harness.plan_json_repair.enabled", json={"enabled": True}).status_code == 422
    assert c.put("/v1/flags/other.x", json={"enabled": True, "reason": "abc", "introduced_for": "m", "removal_condition": "abc"}).status_code == 422
    for k in ("harness.plan_json_repair.enabled", "harness.plan_heuristic_fallback.enabled"):
        assert c.put(f"/v1/flags/{k}", json={"enabled": True, "reason": "약한 플래너 군말 보정", "introduced_for": "mock-planner-sloppy",
                                                 "removal_condition": "ablation removable"}).status_code == 200
    doc = json.loads(Path(tmp_path / "flags.json").read_text())
    f = doc["flags"]["harness.plan_json_repair.enabled"]
    assert f["defaultVariant"] == "on" and f["metadata"]["introducedFor"] == "mock-planner-sloppy"
    # ablation: JSON 복구를 끄면 품질 붕괴(keep), 휴리스틱 폴백은 꺼도 같음(removable)
    ab = c.post("/v1/ablations", json={"suite": agent_suite(), "flags": ["harness.plan_json_repair.enabled", "harness.plan_heuristic_fallback.enabled"]}).json()
    rec = {v["variant"]: v["recommendation"] for v in ab["variants"]}
    assert rec == {"off:harness.plan_json_repair.enabled": "keep", "off:harness.plan_heuristic_fallback.enabled": "removable", "all-off": "keep"}
    assert ab["removable"] == ["harness.plan_heuristic_fallback.enabled"]


def test_flags_client_ofrep_and_overrides():
    import asyncio
    import os
    seen = []

    def ofrep(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"key": req.url.path.rsplit("/", 1)[1], "value": False, "reason": "STATIC"})
    flags.transport = httpx.MockTransport(ofrep)
    flags.clear_cache()
    os.environ["ATLAS_FLAGD_URL"] = "http://flagd:8016"
    try:
        assert asyncio.run(flags.enabled("harness.a.enabled", default=True)) is False
        assert asyncio.run(flags.enabled("harness.a.enabled", default=True)) is False and len(seen) == 1   # 캐시

        async def scoped():
            with flags.overrides({"harness.a.enabled": True, "guard.off": True}):
                return await flags.enabled("harness.a.enabled"), flags.active_overrides()
        val, act = asyncio.run(scoped())
        assert val is True and act == {"harness.a.enabled": True}                 # harness.* 만 덮어쓰기 허용
    finally:
        flags.transport = None
        os.environ.pop("ATLAS_FLAGD_URL")
        flags.clear_cache()
    assert asyncio.run(flags.enabled("harness.b.enabled", default=True)) is True  # flagd 없음 → default


def test_best_of_n_and_curve(fx):
    c, _, _ = fx
    msgs = [{"role": "user", "content": "숫자를 골라"}]
    r = c.post("/v1/compute/best-of-n", json={"model": "chat-default", "messages": msgs, "n": 6, "verifier": {"type": "regex", "pattern": r"s=7"}}).json()
    assert (r["status"], r["chosen"], len(r["samples"])) == ("verified", 2, 3)                 # seed 2 → s=7, 조기 종료
    none = c.post("/v1/compute/best-of-n", json={"model": "chat-default", "messages": msgs, "n": 3, "verifier": {"type": "contains", "value": "zzz"}}).json()
    assert none["status"] == "no_verified_sample" and len(none["samples"]) == 3
    cap = c.post("/v1/compute/best-of-n", json={"model": "chat-default", "messages": msgs, "n": 6, "verifier": {"type": "contains", "value": "zzz"}, "max_usd": 0.0015}).json()
    assert cap["status"] == "budget_exhausted" and len(cap["samples"]) == 2
    assert c.post("/v1/compute/best-of-n", json={"model": "x", "messages": msgs, "verifier": {"type": "magic"}}).status_code == 422
    cv = c.post("/v1/compute/curve", json={"suite": SUITE, "efforts": ["low", "medium", "high"]}).json()
    assert cv["recommended"]["effort"] == "low" and [p["effort"] for p in cv["frontier"]] == ["low"]
