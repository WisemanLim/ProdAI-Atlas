"""AL-RUN 외부 호출 — AI Gateway(모든 모델 호출, PRD §1.3-4) · AL-PLAN(A2A 원격 위임)."""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any

import httpx

from atlas_agent_core import otel
from units import al_plan


class GatewayError(Exception):
    pass


class GuardBlocked(Exception):
    """Gateway 가드레일이 입력/출력을 차단 (→ AL-EXIT POLICY_VIOLATION, FR-AL-EXIT-07)."""


class BudgetExceeded(Exception):
    """Gateway 예산 차단 — task_budget_exceeded(과업 토큰, FR-EC-CTRL-03 → BUDGET_TOKENS) · budget_exceeded(계층 예산 → BUDGET_USD)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GatewayClient:
    def __init__(self, base_url: str, key: str, model: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base, self.key, self.model, self.transport = base_url.rstrip("/"), key, model, transport

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tags: str,
                   task_id: str | None = None) -> tuple[dict[str, Any], dict[str, int], Decimal]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
        headers = otel.inject({"Authorization": f"Bearer {self.key}", "x-atlas-tags": tags})
        if task_id:  # 과업(런) 단위 토큰 예산을 Gateway 가 누적 (FR-EC-CTRL-03)
            headers["x-atlas-task-id"] = task_id
        async with httpx.AsyncClient(timeout=60.0, transport=self.transport) as client:
            res = await client.post(f"{self.base}/v1/chat/completions", json=body, headers=headers)
        if res.status_code != 200:
            try:
                code = res.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = ""
            if code == "guard_blocked":
                raise GuardBlocked(res.json()["error"].get("message", "blocked"))
            if code in ("task_budget_exceeded", "budget_exceeded"):
                raise BudgetExceeded(code, res.json()["error"].get("message", code))
            raise GatewayError(f"gateway {res.status_code} {code}")
        data = res.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "content_filter":
            raise GuardBlocked("출력 가드레일 차단")
        u = data.get("usage") or {}
        usage = {"input": int(u.get("prompt_tokens", 0)), "output": int(u.get("completion_tokens", 0))}
        return choice["message"], usage, Decimal(res.headers.get("x-atlas-cost-usd", "0"))


class PlannerClient:
    """ATLAS_PLANNER_URL 이 있으면 AL-PLAN 에 A2A 로 위임, 없으면 같은 규칙의 로컬 분해."""

    def __init__(self, url: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.transport = (url.rstrip("/") if url else None), transport

    async def _send(self, tenant: str, skill: str, data: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                "params": {"tenant": tenant, "metadata": {"skill": skill},
                           "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"data": data}]}}}
        async with httpx.AsyncClient(timeout=60.0, transport=self.transport) as client:
            res = await client.post(f"{self.url}/a2a", json=body, headers=otel.inject({}))
        res.raise_for_status()
        out = res.json()
        if "error" in out:
            raise GatewayError(f"planner {out['error']}")
        task = out["result"]["task"]
        if task["status"]["state"] != "TASK_STATE_COMPLETED":
            note = " ".join(p.get("text", "") for p in (task["status"].get("message") or {}).get("parts", []))
            if "guard_blocked" in note:
                raise GuardBlocked(note)
            raise GatewayError(f"planner task {task['status']['state']}: {note[:200]}")
        return task["artifacts"][0]["parts"][0]["data"]

    async def plan(self, tenant: str, goal: str) -> dict[str, Any]:
        if self.url:
            return await self._send(tenant, "loop.plan", {"goal": goal})
        return al_plan.heuristic_plan(goal, 12)

    async def revise(self, tenant: str, plan: dict[str, Any], digest: str) -> dict[str, Any]:
        if self.url:
            return await self._send(tenant, "plan.revise", {"plan": plan, "observation_digest": digest})
        return al_plan.revise_plan(plan, digest)


class GuardClient:
    """GR-IO 호출 — tool_call 인가(FR-GR-POL-04·05)와 tool_result 정화·spotlight(FR-GR-JB-03·PII)."""

    def __init__(self, base_url: str, tenant: str = "default", transport: httpx.AsyncBaseTransport | None = None,
                 on_error: str = "fail_open"):
        self.base, self.tenant, self.transport, self.on_error = base_url.rstrip("/"), tenant, transport, on_error

    async def check(self, boundary: str, contents: list[str], context: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"tenant": self.tenant, "boundary": boundary, "contents": contents, "context": context or {}}
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as c:
                res = await c.post(f"{self.base}/v1/guard/check", json=body, headers=otel.inject({}))
                res.raise_for_status()
                return res.json()
        except httpx.HTTPError as exc:
            return {"decision": "block" if self.on_error == "fail_closed" else "allow", "reasons": [f"guard unavailable: {exc}"], "unavailable": True}

    async def tool_policy(self, spec: Any, args: dict[str, Any]) -> tuple[str, str]:
        tool = {"name": spec.name, "side_effect": spec.side_effect, "input_schema": spec.input_schema}
        v = await self.check("tool_call", [""], {"tool": tool, "args": args})
        mapping = {"block": "deny", "require_approval": "ask"}
        return mapping.get(v["decision"], "allow"), "; ".join(v.get("reasons", []))

    async def tool_result(self, texts: list[str], session: str) -> tuple[list[str], str | None, str]:
        v = await self.check("tool_result", texts, {"session": session})
        return v.get("contents", texts), v.get("spotlight_rule"), v["decision"]


def from_env() -> tuple[GatewayClient | None, PlannerClient]:
    gw = os.environ.get("ATLAS_GATEWAY_URL")
    model = os.environ.get("ATLAS_AL_RUN_MODEL", "agent-default")
    gateway = GatewayClient(gw, os.environ.get("ATLAS_GATEWAY_KEY", ""), model) if gw else None
    return gateway, PlannerClient(os.environ.get("ATLAS_PLANNER_URL") or None)
