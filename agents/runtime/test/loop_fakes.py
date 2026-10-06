"""루프 시험용 가짜 Gateway — Gateway mock-agent 각본(apps/gateway/internal/provider/mock.go)과 같은 규칙."""
import json
import re
from decimal import Decimal

from units.al_run.clients import GatewayError, GuardBlocked

ARGS = re.compile(r"^\((\{.*?\})\)")


class ScriptGateway:
    def __init__(self, final=None, guard=False, fail_after=None, crash_after=None):
        self.calls, self.final, self.guard, self.fail_after, self.crash_after = 0, final, guard, fail_after, crash_after

    async def chat(self, messages, tools, tags, task_id=None):
        self.task_id = task_id
        self.calls += 1
        if self.guard:
            raise GuardBlocked("입력에서 탈옥 시도 탐지")
        if self.fail_after is not None and self.calls > self.fail_after:
            raise GatewayError("gateway 502 upstream_error")
        if self.crash_after is not None and self.calls > self.crash_after:
            raise SystemExit("worker crashed")  # 프로세스 사망 흉내 — 엔진이 잡지 않는다
        goal = next(m["content"] for m in messages if m["role"] == "user")
        names = [t["function"]["name"] for t in tools]
        results = [m["content"] for m in messages if m["role"] == "tool"]
        last = messages[-1]["role"]
        ms = sorted((goal.index(n), n, (ARGS.match(goal[goal.index(n) + len(n):]) or [None, "{}"])[1]) for n in names if n in goal)
        usage = {"input": sum(len(json.dumps(m)) for m in messages) // 4, "output": 10}

        def call(items, seq):
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{seq}_{i}", "type": "function", "function": {"name": n, "arguments": a}} for i, (_, n, a) in enumerate(items)]}

        if ms and "repeat-forever" in goal:
            return call(ms[:1], len(results)), usage, Decimal("0.0001")
        if ms and "never-finish" in goal:
            _, n, _ = ms[0]
            return call([(0, n, json.dumps({"n": len(results)}))], len(results)), usage, Decimal("0.0001")
        if ms and last != "tool" and not results:
            return call(ms, 0), usage, Decimal("0.0001")
        content = self.final if self.final is not None else ("[mock:final] " + (" | ".join(results) if results else goal))
        return {"role": "assistant", "content": content}, usage, Decimal("0.0002")
