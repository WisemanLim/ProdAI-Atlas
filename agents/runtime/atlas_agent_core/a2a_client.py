"""A2A 클라이언트 (FR-A2A-CLI-01·02·03·04·06·07): 오케스트레이터가 skill 로 대상을 찾아 원격 에이전트에 일을 맡긴다.

- 해석·바인딩(CLI-01): Manager 레지스트리 → 서명 카드(JCS + JWS ES256) 검증, 실패 = untrusted → 위임 차단 · JSONRPC 바인딩 선택
- 원격 위임(CLI-02): delegate(skill|query|agent, message, mode=blocking|async|stream, timeout) — Manager agent-proxy 경유
- 호출자 인증(CLI-03): 자기 에이전트 단기 토큰(aud=atlas-manager)으로 Manager 에 인증, 원격 대상용 테넌트 JWT 는 Manager 가 붙인다
- Task 추적(CLI-04): 스트림 우선 → 끊기면 GetTask 폴링(지수 백오프), 원격 Task 상태를 a2a_remote_tasks 에 미러
- INPUT_REQUIRED(CLI-06): 상위로 되돌리고 resume(task_id, message) 로 같은 Task 에 재전송
- 취소·타임아웃·부분 실패(CLI-07): 마감 초과 시 CancelTask, FAILED·REJECTED·CANCELED 를 구조화 결과로 반환
- 중첩 한도(MA-ORCH-10): 메시지 metadata x-atlas-depth 전파, max_depth 이상이면 위임 거부
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from sqlalchemy import Engine
from sqlalchemy.orm import Session

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}
PAUSED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
BINDING_PREFERENCE = ("JSONRPC", "GRPC", "HTTP+JSON")  # a2a.cli.bindingPreference — 현재 서버는 JSONRPC 만


class Untrusted(Exception):
    """카드 서명 검증 실패 — 위임 차단 (FR-A2A-CARD-02)."""


class DepthExceeded(Exception):
    pass


def canonical(card: dict[str, Any]) -> bytes:
    """JCS(RFC 8785) — Manager(Go) 와 같은 규칙: 키 정렬, 공백 없음, 비ASCII 그대로."""
    body = {k: v for k, v in card.items() if k != "signatures"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class TokenProvider:
    """Manager 호출용 bearer: 서비스 키로 자기 에이전트 단기 토큰을 받아 캐시한다(없으면 무인증 — 개발 모드)."""

    def __init__(self, manager: str, service_key: str | None, state: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        self.manager, self.service, self.state, self.transport = manager.rstrip("/"), service_key or "", state, transport
        self._tok: tuple[str, float] | None = None

    async def __call__(self) -> str | None:
        if not self.service or "CHANGE_ME" in self.service or not self.state.get("agent_id"):
            return None
        if self._tok and self._tok[1] - time.time() > 60:
            return self._tok[0]
        async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as c:
            res = await c.post(f"{self.manager}/api/v1/agents/{self.state['agent_id']}/token", headers={"Authorization": f"Bearer {self.service}"})
        res.raise_for_status()
        b = res.json()
        self._tok = (b["token"], time.time() + float(b.get("expiresIn", 900)))
        return self._tok[0]


class A2AClient:
    def __init__(self, manager_url: str, token_fn=None, *, tenant: str = "default", engine: Engine | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, max_depth: int = 2):
        self.manager, self.token_fn, self.tenant, self.engine = manager_url.rstrip("/"), token_fn, tenant, engine
        self.transport, self.max_depth = transport, max_depth
        self._jwks: tuple[dict[str, Any], float] | None = None

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json", **(extra or {})}
        tok = await self.token_fn() if self.token_fn else None
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    # ---- 해석 (CLI-01 · CARD-03) ----
    async def search(self, q: str | None = None, skill: str | None = None, tag: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"q": q, "skill": skill, "tag": tag, "limit": limit}.items() if v}
        async with self._client() as c:
            res = await c.get(f"{self.manager}/api/v1/a2a/registry/agents", params=params, headers=await self._headers())
        res.raise_for_status()
        return res.json().get("items", [])

    async def _jwk(self, kid: str) -> Any:
        if not self._jwks or time.time() - self._jwks[1] > 300 or not any(k["kid"] == kid for k in self._jwks[0]["keys"]):
            async with self._client() as c:
                res = await c.get(f"{self.manager}/.well-known/jwks.json")
            res.raise_for_status()
            self._jwks = (res.json(), time.time())
        k = next((k for k in self._jwks[0]["keys"] if k["kid"] == kid), None)
        if k is None:
            raise Untrusted(f"알 수 없는 서명 키 {kid}")
        return jwt.PyJWK(k).key

    async def card(self, agent_id: str) -> dict[str, Any]:
        """서명 카드를 받아 검증한다. 서명 없음·불일치·대상 불일치 → Untrusted."""
        async with self._client() as c:
            res = await c.get(f"{self.manager}/api/v1/agents/{agent_id}/card", headers=await self._headers())
        if res.status_code != 200:
            raise Untrusted(f"카드 조회 실패 {res.status_code}")
        card = res.json()
        sigs = card.get("signatures") or []
        if not sigs:
            raise Untrusted("서명 없는 카드")
        s = sigs[0]
        try:
            kid = json.loads(base64.urlsafe_b64decode(s["protected"] + "=" * (-len(s["protected"]) % 4)))["kid"]
            compact = f"{s['protected']}.{_b64(canonical(card))}.{s['signature']}"
            jwt.PyJWS().decode(compact, await self._jwk(kid), algorithms=["ES256"])
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise Untrusted(f"카드 서명 검증 실패: {exc}") from exc
        if (card.get("metadata") or {}).get("atlas.agentId") != agent_id:
            raise Untrusted("카드 대상 불일치")
        if not any(i.get("protocolBinding") in BINDING_PREFERENCE for i in card.get("supportedInterfaces", [])):
            raise Untrusted("지원 바인딩 없음")
        return card

    # ---- 호출 ----
    async def _rpc(self, agent_id: str, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": {**params, "tenant": self.tenant}}
        async with self._client(timeout) as c:
            res = await c.post(f"{self.manager}/api/v1/agents/{agent_id}/a2a", json=body, headers=await self._headers())
        if res.status_code >= 400:
            raise httpx.HTTPStatusError(f"proxy {res.status_code}: {res.text[:200]}", request=res.request, response=res)
        return res.json()

    def _mirror(self, agent: dict[str, Any], task: dict[str, Any], parent: str | None, skill: str | None, detail: dict[str, Any] | None = None) -> None:
        if self.engine is None or not task.get("id"):
            return
        from atlas_agent_core.push import RemoteTaskRow
        now = datetime.now(UTC)
        with Session(self.engine) as s, s.begin():
            row = s.get(RemoteTaskRow, (self.tenant, task["id"]))
            if row is None:
                row = RemoteTaskRow(tenant=self.tenant, remote_task_id=task["id"], agent_id=agent["agentId"], agent_name=agent.get("name", ""),
                                    parent=parent, skill=skill, state=task["status"]["state"], detail=detail, created_at=now, updated_at=now)
                s.add(row)
            else:
                row.state, row.updated_at, row.detail = task["status"]["state"], now, detail or row.detail

    async def _track(self, agent: dict[str, Any], task: dict[str, Any], deadline: float, parent: str | None, skill: str | None) -> tuple[dict[str, Any], str]:
        delay = 0.2
        while task["status"]["state"] not in TERMINAL | PAUSED:
            if time.monotonic() >= deadline:
                out = await self._rpc(agent["agentId"], "CancelTask", {"id": task["id"]})
                if "error" in out:  # -32002 이미 끝남 → 최신 상태로
                    got = await self._rpc(agent["agentId"], "GetTask", {"id": task["id"]})
                    return got.get("result", task), "timeout_not_cancelable"
                self._mirror(agent, out["result"], parent, skill, {"reason": "timeout"})
                return out["result"], "timeout"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)
            got = await self._rpc(agent["agentId"], "GetTask", {"id": task["id"]})
            task = got.get("result", task)
            self._mirror(agent, task, parent, skill)
        return task, ""

    async def _stream(self, agent: dict[str, Any], params: dict[str, Any], deadline: float) -> dict[str, Any] | None:
        """SendStreamingMessage — 최종 Task 를 돌려주거나, 스트림이 끊기면 마지막으로 본 Task(None 가능)."""
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendStreamingMessage", "params": {**params, "tenant": self.tenant}}
        task: dict[str, Any] | None = None
        try:
            async with self._client(max(1.0, deadline - time.monotonic())) as c, \
                    c.stream("POST", f"{self.manager}/api/v1/agents/{agent['agentId']}/a2a", json=body, headers=await self._headers()) as res:
                async for line in res.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    msg = json.loads(line[6:])
                    r = msg.get("result") or {}
                    if "error" in msg:
                        return {"_error": msg["error"]}
                    if "task" in r:
                        task = r["task"]
                    elif "artifactUpdate" in r and task is not None:
                        task.setdefault("artifacts", []).append(r["artifactUpdate"]["artifact"])
                    elif "statusUpdate" in r and task is not None:
                        task["status"] = r["statusUpdate"]["status"]
                        if r["statusUpdate"].get("final"):
                            return task
        except (httpx.HTTPError, ValueError):
            pass
        return task

    @staticmethod
    def _message(message: str | dict[str, Any], depth: int, parent: str | None, context_id: str | None, task_id: str | None = None) -> dict[str, Any]:
        parts = [{"text": message}] if isinstance(message, str) else [{"data": message}]
        msg: dict[str, Any] = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": parts,
                               "metadata": {"x-atlas-depth": depth + 1, **({"x-atlas-parent": parent} if parent else {})}}
        if context_id:
            msg["contextId"] = context_id
        if task_id:
            msg["taskId"] = task_id
        return msg

    def _result(self, agent: dict[str, Any], task: dict[str, Any], reason: str, started: float, skill: str | None) -> dict[str, Any]:
        data = next((p.get("data") for a in task.get("artifacts") or [] for p in a.get("parts", []) if "data" in p), None)
        text = " ".join(p.get("text", "") for p in ((task.get("status") or {}).get("message") or {}).get("parts", []))
        state = (task.get("status") or {}).get("state", "")
        return {"agent": {"id": agent["agentId"], "name": agent.get("name")}, "skill": skill, "taskId": task.get("id"),
                "contextId": task.get("contextId"), "state": state, "reason": reason or {"TASK_STATE_COMPLETED": "", "TASK_STATE_INPUT_REQUIRED": "input_required"}.get(state, state.split("_")[-1].lower()),
                "output": data, "message": text, "elapsed_s": round(time.monotonic() - started, 3)}

    async def delegate(self, message: str | dict[str, Any], *, skill: str | None = None, query: str | None = None, agent: str | None = None,
                       mode: str = "blocking", timeout: float = 60.0, depth: int = 0, parent: str | None = None,
                       context_id: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        if depth >= self.max_depth:
            raise DepthExceeded(f"위임 깊이 {depth} ≥ max_depth {self.max_depth}")
        if agent:
            target = {"agentId": agent, "name": agent}
        else:
            found = await self.search(q=query, skill=skill, limit=1)
            if not found:
                return {"agent": None, "skill": skill, "state": "TASK_STATE_REJECTED", "reason": "no_agent", "output": None, "elapsed_s": 0.0}
            target = found[0]
        try:
            card = await self.card(target["agentId"])
        except Untrusted as exc:
            return {"agent": {"id": target["agentId"], "name": target.get("name")}, "skill": skill, "state": "TASK_STATE_REJECTED",
                    "reason": "untrusted_card", "message": str(exc), "output": None, "elapsed_s": round(time.monotonic() - started, 3)}
        skill_id = skill or next((s["id"] for s in card.get("skills", [])), None)
        params = {"message": self._message(message, depth, parent, context_id), "metadata": {"skill": skill_id},
                  "configuration": {"returnImmediately": True}}
        deadline = time.monotonic() + timeout
        task: dict[str, Any] | None = None
        if mode == "stream":
            task = await self._stream(target, params, deadline)
            if task and "_error" in task:
                return {**self._result(target, {"status": {"state": "TASK_STATE_REJECTED"}}, "rpc_error", started, skill_id), "message": str(task["_error"])}
        if task is None:
            out = await self._rpc(target["agentId"], "SendMessage", params)
            if "error" in out:
                return {**self._result(target, {"status": {"state": "TASK_STATE_REJECTED"}}, "rpc_error", started, skill_id), "message": out["error"].get("message")}
            task = out["result"]["task"]
        self._mirror(target, task, parent, skill_id)
        if mode == "async":
            return self._result(target, task, "submitted", started, skill_id)
        reason = ""
        if task["status"]["state"] not in TERMINAL | PAUSED:
            task, reason = await self._track(target, task, deadline, parent, skill_id)
        self._mirror(target, task, parent, skill_id)
        return self._result(target, task, reason, started, skill_id)

    async def resume(self, agent_id: str, task_id: str, message: str | dict[str, Any], *, timeout: float = 60.0, depth: int = 0) -> dict[str, Any]:
        started = time.monotonic()
        out = await self._rpc(agent_id, "SendMessage", {"message": self._message(message, depth, None, None, task_id), "configuration": {"returnImmediately": True}})
        target = {"agentId": agent_id, "name": agent_id}
        if "error" in out:
            return {**self._result(target, {"status": {"state": "TASK_STATE_REJECTED"}}, "rpc_error", started, None), "message": out["error"].get("message")}
        task, reason = await self._track(target, out["result"]["task"], time.monotonic() + timeout, None, None)
        return self._result(target, task, reason, started, None)

    async def get(self, agent_id: str, task_id: str) -> dict[str, Any]:
        return (await self._rpc(agent_id, "GetTask", {"id": task_id})).get("result", {})

    async def cancel(self, agent_id: str, task_id: str) -> dict[str, Any]:
        return await self._rpc(agent_id, "CancelTask", {"id": task_id})
