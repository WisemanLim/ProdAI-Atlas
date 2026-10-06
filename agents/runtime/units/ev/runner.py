"""평가 실행기 — 타깃 어댑터(prompt·agent·http, FR-EV-REG-02) · 반복 시행 pass^k(FR-EV-REG-04) · 게이트(FR-EV-REG-06)."""
from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from typing import Any

import httpx

from atlas_agent_core import otel
from units.ev import judge as judge_mod
from units.ev import scorers
from units.ev.suite import render


def pass_hat_k(n: int, c: int, k: int) -> float:
    """비편향 추정 C(c,k)/C(n,k) — k 번 시행이 모두 성공할 확률."""
    if k > n:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


async def call_target(t: dict[str, Any], item: dict[str, Any], run_id: str, client: httpx.AsyncClient) -> tuple[Any, float]:
    """(출력, 비용 USD). 모든 모델 호출은 Gateway 경유 — 비용은 eval_run 태그로 귀속."""
    if t["type"] == "prompt":
        msgs = ([{"role": "system", "content": t["system"]}] if t.get("system") else []) + [{"role": "user", "content": render(t["template"], item)}]
        body: dict[str, Any] = {"model": t["model"], "messages": msgs, "temperature": 0}
        if t.get("response_format"):
            body["response_format"] = {"type": t["response_format"]}
        if t.get("effort"):                       # 품질-계산 곡선 (BL-COMP-06)
            body["atlas"] = {"effort": t["effort"]}
        gw = os.environ.get("ATLAS_GATEWAY_URL", "http://gateway:8080").rstrip("/")
        headers = otel.inject({"Authorization": f"Bearer {os.environ.get('ATLAS_GATEWAY_KEY', '')}",
                               "x-atlas-tags": f"agent_id:ev-reg,unit_id:EV-REG,eval_run:{run_id}"})
        res = await client.post(f"{gw}/v1/chat/completions", json=body, headers=headers)
        res.raise_for_status()
        return res.json()["choices"][0]["message"].get("content") or "", float(res.headers.get("x-atlas-cost-usd", "0"))
    if t["type"] == "agent":
        data = render(t["data_template"], item) if t.get("data_template") else item["input"]
        parts = [{"data": data}] if isinstance(data, dict) else [{"text": str(data)}]
        body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                "params": {"metadata": {"skill": t["skill"]} if t.get("skill") else {},
                           "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": parts,
                                       **({"metadata": t["metadata"]} if t.get("metadata") else {})}}}   # 보정 플래그·프롬프트 버전 고정 (BL-HARN-05·06)
        res = await client.post(t["url"].rstrip("/") + "/a2a", json=body, headers=otel.inject({}))
        res.raise_for_status()
        out = res.json()
        if "error" in out:
            raise RuntimeError(f"A2A {out['error']['code']}: {out['error']['message']}")
        task = out["result"]["task"]
        if task["status"]["state"] != "TASK_STATE_COMPLETED":
            raise RuntimeError(f"task {task['status']['state']}")
        part = task["artifacts"][0]["parts"][0]
        result = part.get("data", part.get("text"))
        if t.get("result_path"):
            from units.ev.suite import dig
            result = dig(result, t["result_path"])
        return result, 0.0
    res = await client.post(t["url"], json=item["input"])
    res.raise_for_status()
    return res.text, 0.0


async def judge_score(s: dict[str, Any], j: Any, output: Any, item: dict[str, Any], run_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    r = await judge_mod.binary(client, j, output, item.get("input"), item.get("expected_output"), f"eval_run:{run_id}")
    return {"type": "judge", "judge": j.name, "version": j.version, "passed": r["passed"], "score": r["score"], "critique": r["critique"][:500],
            "cost_usd": r["cost_usd"], **({"agreement": r["agreement"]} if "agreement" in r else {})}


async def execute(suite: dict[str, Any], items: list[dict[str, Any]], run_id: str,
                  transport: httpx.AsyncBaseTransport | None = None, judges: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    judges = judges or {}
    sem = asyncio.Semaphore(int(suite.get("max_concurrency", os.environ.get("ATLAS_EV_MAX_CONCURRENCY", "16"))))
    k = int(suite["epochs"])

    async with httpx.AsyncClient(timeout=120, transport=transport) as client:
        async def one(item: dict[str, Any], t: dict[str, Any], epoch: int) -> dict[str, Any]:
            async with sem:
                t0 = time.perf_counter()
                try:
                    output, cost = await call_target(t, item, run_id, client)
                    err = None
                except Exception as exc:  # noqa: BLE001 — 실행 오류는 해당 시행 실패로 기록
                    output, cost, err = None, 0.0, f"{type(exc).__name__}: {exc}"[:300]
                latency = round((time.perf_counter() - t0) * 1000, 2)
                scores = []
                if not err:
                    for s in suite["scorers"]:
                        if s["type"] == "judge":
                            try:
                                scores.append(await judge_score(s, judges[s["judge"]], output, item, run_id, client))
                            except Exception as exc:  # noqa: BLE001 — judge 오류는 그 채점 실패
                                scores.append({"type": "judge", "judge": s["judge"], "passed": False, "score": 0.0, "critique": f"judge 오류: {exc}"[:300]})
                        else:
                            scores.append(scorers.score(s, output, item, latency, cost))
                return {"run_id": run_id, "item_id": item["item_id"], "target": t["id"], "epoch": epoch,
                        "passed": err is None and all(s["passed"] for s in scores), "scores": scores,
                        "output": None if output is None else scorers.as_text(output)[:4000], "latency_ms": latency, "cost_usd": cost, "error": err}

        results = await asyncio.gather(*(one(i, t, e) for i in items for t in suite["targets"] for e in range(k)))

    per_target: dict[str, Any] = {}
    for t in suite["targets"]:
        rs = [r for r in results if r["target"] == t["id"]]
        by_item: dict[str, list[bool]] = {}
        for r in rs:
            by_item.setdefault(r["item_id"], []).append(r["passed"])
        hats = [pass_hat_k(len(v), sum(v), k) for v in by_item.values()]
        per_target[t["id"]] = {"results": len(rs), "passed": sum(r["passed"] for r in rs),
                               "pass_rate": round(sum(r["passed"] for r in rs) / max(1, len(rs)), 4),
                               "pass_at_1": round(sum(sum(v) / len(v) for v in by_item.values()) / max(1, len(by_item)), 4),
                               "pass_hat_k": round(sum(hats) / max(1, len(hats)), 4),
                               "errors": sum(1 for r in rs if r["error"]), "cost_usd": round(sum(r["cost_usd"] for r in rs), 8),
                               "p95_latency_ms": sorted(r["latency_ms"] for r in rs)[int(0.95 * (len(rs) - 1))] if rs else 0}
    total = len(results)
    summary = {"items": len(items), "targets": len(suite["targets"]), "epochs": k, "results": total,
               "pass_rate": round(sum(r["passed"] for r in results) / max(1, total), 4),
               "pass_hat_k": round(min((v["pass_hat_k"] for v in per_target.values()), default=0), 4),
               "errors": sum(1 for r in results if r["error"]), "cost_usd": round(sum(r["cost_usd"] for r in results), 8),
               "per_target": per_target}
    return results, summary


def gate(suite: dict[str, Any], summary: dict[str, Any], baseline: dict[str, Any] | None) -> tuple[str, list[str]]:
    """결정적 판정 임계 + 기준선 대비 하락 허용폭 (FR-EV-REG-06)."""
    th, reasons = suite.get("thresholds", {}), []
    if summary["pass_rate"] < th.get("pass_rate", 1.0):
        reasons.append(f"pass_rate {summary['pass_rate']} < {th.get('pass_rate', 1.0)}")
    if "pass_hat_k" in th and summary["pass_hat_k"] < th["pass_hat_k"]:
        reasons.append(f"pass^k {summary['pass_hat_k']} < {th['pass_hat_k']}")
    if baseline is not None and "max_drop_pp" in th:
        drop = (baseline["pass_rate"] - summary["pass_rate"]) * 100
        if drop > th["max_drop_pp"]:
            reasons.append(f"기준선 대비 {drop:.1f}pp 하락 > {th['max_drop_pp']}pp")
    return ("fail" if reasons else "pass"), reasons

