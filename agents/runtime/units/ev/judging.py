"""EV-JUDGE · EV-SCORE API — judge 정의·평가·보정·shadow, 점수 diff·추세·드리프트, 온라인 샘플링 채점, 피드백, 스코어카드.

  POST /v1/judges · GET /v1/judges[/{name}] · POST /v1/judges/{name}/evaluate · /calibrate · /shadow
  GET /v1/runs/{a}/diff/{b} · GET /v1/scores · /v1/scores/trend · /v1/scores/drift · GET /v1/scorecard
  POST·GET /v1/online-rules · POST /v1/online-rules/{id}/run · POST /v1/feedback
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from atlas_agent_core import otel
from units.ev import judge as J
from units.ev import stats
from units.ev.store import IN_REVIEW

log = logging.getLogger("atlas.ev")
router = APIRouter(prefix="/v1", tags=["judge", "scores"])
CANDIDATES = "online-candidates"      # 저점 trace·부정 피드백이 모이는 기본 골든셋 (EV-SCORE-07)


def E():
    from units import ev  # 순환 import 회피 (ev/__init__ 가 이 모듈을 라우터로 싣는다)
    return ev


def judge_view(j) -> dict[str, Any]:
    return {"name": j.name, "version": j.version, "mode": j.mode, "rubric_md": j.rubric_md, "instructions": j.instructions, "model": j.model,
            "jury": j.jury, "threshold": j.threshold, "calibration": j.calibration, "created_at": j.created_at.isoformat()}


def need_judge(name: str, version: int | None = None):
    j = E().store().judge(name, version)
    if j is None:
        raise HTTPException(404, "judge not found")
    return j


class JudgeIn(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,128}$")
    mode: str = Field("binary", pattern="^(binary|pairwise|rag)$")
    rubric_md: str = Field(min_length=1)
    instructions: str = ""
    model: str = "judge-default"
    jury: list[str] = []
    threshold: float = Field(0.8, ge=0, le=1)


@router.post("/judges", status_code=201)
async def create_judge(body: JudgeIn):
    """루브릭 judge 정의 (FR-EV-JUDGE-01) — 같은 이름으로 다시 만들면 새 버전(이전 버전 불변)."""
    if body.jury and len(body.jury) < 3:
        raise HTTPException(422, "배심원은 3개 이상 (FR-EV-JUDGE-04)")
    j = E().store().add_judge(body.name, **body.model_dump(exclude={"name"}))
    return judge_view(j)


@router.get("/judges")
async def list_judges():
    return {"items": [judge_view(j) for j in E().store().judges()]}


@router.get("/judges/{name}")
async def get_judge(name: str, version: int | None = None):
    return judge_view(need_judge(name, version))


@router.post("/judges/{name}/evaluate")
async def evaluate(name: str, body: dict[str, Any], version: int | None = None):
    """즉석 판정 — binary {output, input?, expected?} · pairwise {output_a, output_b} · rag {output, contexts, expected_contexts?}."""
    j = need_judge(name, version)
    async with E().http_client(60) as c:
        out = await J.evaluate(c, j, body)
    return {"judge": j.name, "version": j.version, **out}


class CalItem(BaseModel):
    output: Any
    human: str = Field(pattern="^(pass|fail)$")
    input: Any = None
    expected: Any = None


class Calibrate(BaseModel):
    items: list[CalItem] = Field(min_length=5)
    min_kappa: float = Field(float(os.environ.get("ATLAS_EV_JUDGE_MIN_KAPPA", "0.6")), ge=0, le=1)


@router.post("/judges/{name}/calibrate")
async def calibrate(name: str, body: Calibrate, version: int | None = None):
    """사람 일치도 보정 (FR-EV-JUDGE-05): 사람 라벨 vs judge 판정 → 일치율·Cohen κ. κ ≥ 기준이면 trusted(게이트 사용 가능)."""
    j = need_judge(name, version)
    if j.mode != "binary":
        raise HTTPException(422, "보정은 binary judge 만")
    async with E().http_client(60) as c:
        got = await asyncio.gather(*(J.binary(c, j, it.output, it.input, it.expected, "eval_run:calibration") for it in body.items))
    human = [it.human == "pass" for it in body.items]
    judged = [g["passed"] for g in got]
    kappa = stats.cohen_kappa(human, judged)
    cal = {"n": len(human), "agreement": round(sum(h == x for h, x in zip(human, judged)) / len(human), 4), "kappa": kappa,
           "min_kappa": body.min_kappa, "trusted": kappa >= body.min_kappa,
           "disagreements": [i for i, (h, x) in enumerate(zip(human, judged)) if h != x]}
    E().store().set_calibration(j.name, j.version, cal)
    return {"judge": j.name, "version": j.version, **cal}


class Shadow(BaseModel):
    from_version: int
    to_version: int
    limit: int = Field(200, ge=1, le=2000)


@router.post("/judges/{name}/shadow")
async def shadow(name: str, body: Shadow):
    """criteria drift (FR-EV-JUDGE-06): 직전 버전으로 채점된 표본을 새 버전으로 재채점해 판정 변경률을 본다(원래 점수는 그대로)."""
    old, new = need_judge(name, body.from_version), need_judge(name, body.to_version)
    rows = [r for r in E().store().scores(scorer=f"judge:{name}") if r.judge_version == old.version and r.payload][-body.limit:]
    if not rows:
        raise HTTPException(409, "재채점할 표본 없음 (이 버전으로 채점된 점수가 없음)")
    async with E().http_client(60) as c:
        again = await asyncio.gather(*(J.binary(c, new, r.payload.get("output"), r.payload.get("input"), r.payload.get("expected"),
                                                 "eval_run:shadow") for r in rows))
    flips = [{"score_id": r.id, "item_id": r.item_id, "trace_id": r.trace_id, "before": r.passed, "after": g["passed"]}
             for r, g in zip(rows, again) if bool(r.passed) != g["passed"]]
    E().store().add_scores([{"source": "shadow", "run_id": r.run_id, "item_id": r.item_id, "trace_id": r.trace_id, "target": r.target,
                             "scorer": f"judge:{name}", "judge_version": new.version, "value": g["score"], "passed": g["passed"],
                             "critique": g["critique"], "payload": r.payload} for r, g in zip(rows, again)])
    rate = round(len(flips) / len(rows), 4)
    return {"judge": name, "from_version": old.version, "to_version": new.version, "sampled": len(rows), "changed": len(flips),
            "change_rate": rate, "flips": flips[:100], "pass_to_fail": sum(f["before"] and not f["after"] for f in flips),
            "fail_to_pass": sum((not f["before"]) and f["after"] for f in flips)}


# ---- EV-SCORE ----

def item_pass(rid: str) -> dict[tuple[str, str], float]:
    """(item, target) → 시행 통과율 (epochs 평균)."""
    acc: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for x in E().store().results(rid):
        acc[(x.item_id, x.target)].append(x.passed)
    return {k: sum(v) / len(v) for k, v in acc.items()}


@router.get("/runs/{base}/diff/{new}")
async def diff(base: str, new: str):
    """실험 diff (FR-EV-SCORE-02·03): 항목 조인 → 전이 행렬 · 퇴행 우선 목록 · McNemar · 부트스트랩 95% CI."""
    st = E().store()
    rb, rn = st.run(base), st.run(new)
    if rb is None or rn is None:
        raise HTTPException(404, "run not found")
    pb, pn = item_pass(base), item_pass(new)
    tb = {i for i, _ in pb}
    tn = {i for i, _ in pn}
    keys = sorted({(i, t) for (i, t) in pb} & {(i, t) for (i, t) in pn}) or sorted(
        {(i, None) for i in tb & tn})                                     # 타깃 이름이 다르면 항목 단위로
    if keys and keys[0][1] is None:
        agg = lambda p, i: sum(v for (ii, _), v in p.items() if ii == i) / max(1, sum(1 for (ii, _) in p if ii == i))
        pairs = [(i, None, agg(pb, i), agg(pn, i)) for i, _ in keys]
    else:
        pairs = [(i, t, pb[(i, t)], pn[(i, t)]) for i, t in keys]
    matrix = {"pass_pass": 0, "pass_fail": 0, "fail_pass": 0, "fail_fail": 0}
    items = []
    for i, t, a, b in pairs:
        sa, sb = "pass" if a >= 0.5 else "fail", "pass" if b >= 0.5 else "fail"
        matrix[f"{sa}_{sb}"] += 1
        if sa != sb or a != b:
            items.append({"item_id": i, "target": t, "base": round(a, 4), "new": round(b, 4),
                          "change": "regression" if sa == "pass" and sb == "fail" else "improvement" if sa == "fail" and sb == "pass" else "shift"})
    items.sort(key=lambda x: {"regression": 0, "shift": 1, "improvement": 2}[x["change"]])
    p = stats.mcnemar(matrix["pass_fail"], matrix["fail_pass"])
    lo, hi = stats.bootstrap_diff_ci([a for *_, a, _ in pairs], [b for *_, b in pairs])
    delta = round((sum(b for *_, b in pairs) - sum(a for *_, a, _ in pairs)) / max(1, len(pairs)), 4)
    return {"base": base, "new": new, "paired_items": len(pairs), "matrix": matrix, "delta_pass_rate": delta, "ci95": [lo, hi],
            "mcnemar_p": p, "significant": p < 0.05 and not (lo <= 0 <= hi), "items": items[:500],
            "verdict": "regressed" if matrix["pass_fail"] and delta < 0 else "improved" if delta > 0 else "unchanged"}


@router.get("/scores")
async def list_scores(scorer: str | None = None, source: str | None = None, run_id: str | None = None, trace_id: str | None = None,
                      limit: int = 200):
    rows = E().store().scores(scorer=scorer, source=source, run_id=run_id, trace_id=trace_id)[-min(limit, 2000):]
    return {"items": [{"id": r.id, "source": r.source, "run_id": r.run_id, "item_id": r.item_id, "trace_id": r.trace_id, "target": r.target,
                       "scorer": r.scorer, "judge_version": r.judge_version, "value": r.value, "passed": r.passed, "critique": r.critique,
                       "created_at": r.created_at.isoformat()} for r in rows]}


@router.get("/scores/trend")
async def trend(suite: str | None = None, scorer: str | None = None, target: str | None = None):
    """추세 (FR-EV-SCORE-04): 실행 순서(시간·데이터셋 버전) 축으로 통과율·채점기 평균."""
    points = []
    for r in E().store().runs_of(suite):
        rows = [x for x in E().store().scores(run_id=r.id) if (scorer is None or x.scorer == scorer) and (target is None or x.target == target)]
        pt = (r.summary or {}).get("per_target", {}).get(target, r.summary or {}) if target else (r.summary or {})
        points.append({"run_id": r.id, "suite": r.suite_name, "dataset_version": r.dataset_version, "created_at": r.created_at.isoformat(),
                       "gate": r.gate, "pass_rate": pt.get("pass_rate"), "pass_hat_k": pt.get("pass_hat_k"),
                       "scorer_mean": round(sum(x.value for x in rows) / len(rows), 4) if rows else None})
    return {"suite": suite, "scorer": scorer, "points": points}


@router.get("/scores/drift")
async def drift(scorer: str, source: str = "online", baseline: int = 20):
    """온라인 드리프트 (FR-EV-SCORE-05): 시간순 점수의 EWMA 가 기준 구간 관리 한계를 넘으면 변화점."""
    vals = [r.value for r in E().store().scores(scorer=scorer, source=source)]
    return {"scorer": scorer, "source": source, **stats.ewma_drift(vals, baseline=baseline)}


@router.get("/scorecard")
async def scorecard(suite: str | None = None):
    """스코어카드 (FR-EV-SCORE-09): 타깃(에이전트·프롬프트 버전)별 최신 실행의 성공률·pass^k·과업당 비용·p95."""
    latest: dict[str, dict[str, Any]] = {}
    for r in E().store().runs_of(suite):
        for t, v in ((r.summary or {}).get("per_target") or {}).items():
            n = max(1, v.get("results", 0))
            latest[t] = {"target": t, "suite": r.suite_name, "run_id": r.id, "dataset_version": r.dataset_version, "gate": r.gate,
                         "task_success_rate": v.get("pass_rate"), "pass_hat_k": v.get("pass_hat_k"), "cost_per_task_usd": round(v.get("cost_usd", 0) / n, 8),
                         "p95_latency_ms": v.get("p95_latency_ms"), "errors": v.get("errors"), "at": r.created_at.isoformat()}
    return {"items": sorted(latest.values(), key=lambda x: -(x["task_success_rate"] or 0))}


# ---- 온라인 judge (EV-JUDGE-09) · 저점 → 골든셋 (EV-SCORE-07) · 피드백 (EV-SCORE-08) ----

class RuleIn(BaseModel):
    name: str
    filter: str = Field(min_length=1)
    sample_rate: float = Field(0.1, gt=0, le=1)
    judge: str
    dataset: str | None = CANDIDATES
    tenant: str = "default"
    enabled: bool = True


def rule_view(r) -> dict[str, Any]:
    return {"id": r.id, "tenant": r.tenant, "name": r.name, "filter": r.filter, "sample_rate": r.sample_rate, "judge": r.judge,
            "dataset": r.dataset, "enabled": r.enabled, "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None}


@router.post("/online-rules", status_code=201)
async def create_rule(body: RuleIn):
    need_judge(body.judge)
    return rule_view(E().store().add_rule(**body.model_dump()))


@router.get("/online-rules")
async def list_rules():
    return {"items": [rule_view(r) for r in E().store().rules()]}


def sampled(trace_id: str, rate: float) -> bool:
    """결정적 표본 추출 — 같은 trace 는 같은 결정(재실행해도 표본이 흔들리지 않는다)."""
    return int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


async def candidate(trace_id: str, dataset: str, tenant: str, tags: list[str]) -> str | None:
    """저점·부정 trace → 골든셋 후보(in_review). 데이터셋이 없으면 만든다. 이미 있는 trace 면 건너뛴다."""
    ev = E()
    d = ev.store().dataset(dataset) or ev.store().create_dataset(dataset, "온라인 저점·부정 피드백 trace 후보 (EV-SCORE-07)", None)
    if any(i.source_trace_id == trace_id for i in ev.store().items(d.id)):
        return None
    try:
        item = await ev.from_trace(dataset, ev.FromTrace(trace_id=trace_id, tags=tags), tenant)
    except HTTPException as exc:
        log.info("후보 생성 건너뜀 trace=%s: %s", trace_id, exc.detail)
        return None
    return item["id"]


async def run_rule(rule) -> dict[str, Any]:
    ev = E()
    j = need_judge(rule.judge)
    ob = os.environ.get("ATLAS_OB_URL", "http://agent-ob:8000").rstrip("/")
    seen = {r.trace_id for r in ev.store().scores(scorer=f"judge:{j.name}", source="online", tenant=rule.tenant)}
    out = {"rule": rule.id, "fetched": 0, "sampled": 0, "scored": 0, "failed": 0, "candidates": []}
    async with ev.http_client(60) as c:
        res = await c.get(f"{ob}/v1/traces", params={"q": rule.filter, "since": 86400, "limit": 200}, headers=otel.inject({}))
        res.raise_for_status()
        traces = res.json().get("items", [])
        out["fetched"] = len(traces)
        rows = []
        for t in traces:
            tid = t.get("trace_id") or t.get("traceId")
            if not tid or tid in seen or not sampled(tid, rule.sample_rate):
                continue
            out["sampled"] += 1
            io = await c.get(f"{ob}/v1/traces/{tid}/io", headers=otel.inject({}))
            if io.status_code != 200 or not io.json().get("captured"):
                continue
            body = io.json()
            g = await J.binary(c, j, body.get("output"), body.get("input"), None, "eval_run:online")
            rows.append({"tenant": rule.tenant, "source": "online", "trace_id": tid, "scorer": f"judge:{j.name}", "judge_version": j.version,
                         "value": g["score"], "passed": g["passed"], "critique": g["critique"],
                         "payload": {"input": body.get("input"), "output": body.get("output")}})
            out["scored"] += 1
            if not g["passed"]:
                out["failed"] += 1
                if rule.dataset and (cid := await candidate(tid, rule.dataset, rule.tenant, ["online-low-score", f"judge:{j.name}"])):
                    out["candidates"].append(cid)
    ev.store().add_scores(rows)
    ev.store().touch_rule(rule.id)
    return out


@router.post("/online-rules/{rid}/run")
async def run_now(rid: str):
    r = E().store().rule(rid)
    if r is None:
        raise HTTPException(404, "rule not found")
    return await run_rule(r)


async def online_loop(interval_s: float = 60.0) -> None:
    """활성 규칙을 주기 실행한다 (프로세스 수명 동안). 실패는 기록만."""
    while True:
        await asyncio.sleep(interval_s)
        for r in E().store().rules(enabled_only=True):
            try:
                await run_rule(r)
            except Exception as exc:  # noqa: BLE001
                log.warning("online rule %s 실패: %s", r.id, exc)


class FeedbackIn(BaseModel):
    trace_id: str
    name: str = "user"
    value: float | str
    comment: str | None = None
    tenant: str = "default"
    dataset: str | None = CANDIDATES


@router.post("/feedback", status_code=201)
async def feedback(body: FeedbackIn):
    """사용자 피드백 (FR-EV-SCORE-08): 엄지·별점·코멘트 → trace 에 점수로. 부정(👎·≤0) 은 골든셋 후보로 (EV-SCORE-07)."""
    v = {"up": 1.0, "down": 0.0, "thumbs_up": 1.0, "thumbs_down": 0.0}.get(str(body.value).lower()) if isinstance(body.value, str) else float(body.value)
    if v is None:
        raise HTTPException(422, "value = 숫자 또는 up|down")
    negative = v <= 0 or (body.name == "stars" and v <= 2)
    E().store().add_scores([{"tenant": body.tenant, "source": "feedback", "trace_id": body.trace_id, "scorer": f"feedback:{body.name}",
                             "value": v, "passed": not negative, "critique": body.comment}])
    cid = await candidate(body.trace_id, body.dataset, body.tenant, ["user-feedback"]) if negative and body.dataset else None
    return {"trace_id": body.trace_id, "value": v, "negative": negative, "candidate_item": cid, "candidate_status": IN_REVIEW if cid else None}
