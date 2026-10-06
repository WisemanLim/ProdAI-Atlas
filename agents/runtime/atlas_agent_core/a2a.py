"""A2A v1.0 JSON-RPC 2.0 바인딩 (FR-A2A-SRV-02) — SendMessage · GetTask · ListTasks · CancelTask.

SendStreamingMessage · SubscribeToTask · Push 설정은 M1 (capabilities 에 false 로 공시).
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from opentelemetry.trace import SpanKind, Status, StatusCode

from atlas_agent_core import flags, otel, prompts
from atlas_agent_core import tasks as t
from atlas_agent_core.spec import AgentSpec, InputRequired, TaskContext

log = logging.getLogger("atlas.a2a")

# JSON-RPC / A2A 오류 코드
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, UNSUPPORTED_OPERATION = -32001, -32002, -32004

DEFAULT_TENANT = "default"


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def error_body(req_id: Any, err: RpcError) -> dict[str, Any]:
    e: dict[str, Any] = {"code": err.code, "message": err.message}
    if err.data is not None:
        e["data"] = err.data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}


def _agent_message(text: str) -> dict[str, Any]:
    return {"messageId": str(uuid.uuid4()), "role": "ROLE_AGENT", "parts": [{"text": text}]}


def _split_parts(parts: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    texts, data = [], {}
    for p in parts:
        if "text" in p:
            texts.append(str(p["text"]))
        elif isinstance(p.get("data"), dict):
            data.update(p["data"])
    return "\n".join(texts), data


def status_event(row: t.TaskRow) -> dict[str, Any]:
    """A2A StreamResponse.statusUpdate — 종료·입력 대기 상태면 final."""
    st: dict[str, Any] = {"state": row.state, "timestamp": t.iso(row.updated_at)}
    if row.status_message:
        st["message"] = row.status_message
    final = row.state in t.TERMINAL or row.state in (t.INPUT_REQUIRED, t.AUTH_REQUIRED)
    return {"statusUpdate": {"taskId": row.id, "contextId": row.context_id, "status": st, "final": final}}


class A2AHandler:
    def __init__(self, spec: AgentSpec, store: t.TaskStore, auth: Any = None, push: Any = None, card: dict[str, Any] | None = None):
        self.spec, self.store, self.auth = spec, store, auth  # auth: tenancy.TenantAuth | None (FR-PLT-TEN-06)
        self.push = push  # push.Dispatcher | None (FR-A2A-SRV-03)
        self.card = card or {}
        self._background: set[asyncio.Task] = set()
        self._subs: dict[tuple[str, str], set[asyncio.Queue]] = {}

    # ---- 상태 변화 발행: 스트림 구독자 + Push webhook ----
    def _tr(self, tenant: str, task_id: str, to: str, **kw: Any) -> t.TaskRow:
        row = self.store.transition(tenant, task_id, to, **kw)
        self._emit(tenant, row)
        return row

    def _emit(self, tenant: str, row: t.TaskRow) -> None:
        ev = status_event(row)
        for q in list(self._subs.get((tenant, row.id), ())):
            q.put_nowait((ev, row))
        if self.push is not None:
            payload = {**ev, **({"task": t.to_wire(row)} if ev["statusUpdate"]["final"] else {})}
            try:
                self.push.notify(tenant, payload, row.id)
            except Exception:  # 알림 실패가 Task 를 깨지 않는다
                log.exception("push notify failed")

    def _subscribe(self, tenant: str, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault((tenant, task_id), set()).add(q)
        return q

    def _unsubscribe(self, tenant: str, task_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get((tenant, task_id))
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop((tenant, task_id), None)

    async def dispatch(self, body: Any, headers: dict[str, str]) -> dict[str, Any]:
        req_id = body.get("id") if isinstance(body, dict) else None
        try:
            if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
                raise RpcError(INVALID_REQUEST, "jsonrpc 2.0 요청이 아님")
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params 는 객체여야 함")
            method = {"SendMessage": self.send_message, "GetTask": self.get_task,
                      "ListTasks": self.list_tasks, "CancelTask": self.cancel_task,
                      "CreateTaskPushNotificationConfig": self.create_push, "GetTaskPushNotificationConfig": self.get_push,
                      "ListTaskPushNotificationConfig": self.list_push, "DeleteTaskPushNotificationConfig": self.delete_push,
                      "GetExtendedAgentCard": self.extended_card}.get(body["method"])
            if method is None:
                raise RpcError(METHOD_NOT_FOUND, f"지원하지 않는 메서드 {body['method']}")
            await self._authorize(body["method"], params, headers)
            return {"jsonrpc": "2.0", "id": req_id, "result": await method(params, headers)}
        except RpcError as err:
            return error_body(req_id, err)
        except Exception:
            log.exception("a2a internal error")
            return error_body(req_id, RpcError(INTERNAL, "internal error"))

    async def _authorize(self, method: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any] | None:
        """테넌트 토큰 검사 + 카드에 보안 요구를 선언한 skill·확장 카드는 토큰 필수 (FR-A2A-SRV-05)."""
        claims = None
        if self.auth is not None:
            try:
                claims = await self.auth.check(headers, self._tenant(params))
            except PermissionError as exc:
                raise RpcError(exc.args[0], exc.args[1]) from exc
        need = method == "GetExtendedAgentCard"
        if method in ("SendMessage", "SendStreamingMessage"):
            sid = (params.get("metadata") or {}).get("skill") or ((params.get("message") or {}).get("metadata") or {}).get("skill")
            sk = self.spec.skill(sid) if not (params.get("message") or {}).get("taskId") else None
            need = bool(sk and getattr(sk, "security", "") == "required")
        if need and not claims:
            from atlas_agent_core.tenancy import UNAUTHENTICATED
            raise RpcError(UNAUTHENTICATED, "이 skill 은 테넌트 토큰 필요 (카드 securityRequirements)")
        return claims

    @staticmethod
    def _tenant(params: dict[str, Any]) -> str:
        tenant = params.get("tenant") or DEFAULT_TENANT
        if not isinstance(tenant, str) or len(tenant) > 128:
            raise RpcError(INVALID_PARAMS, "tenant 형식 오류")
        return tenant

    def _require(self, tenant: str, task_id: Any) -> t.TaskRow:
        row = self.store.get(tenant, str(task_id)) if task_id else None
        if row is None:
            raise RpcError(TASK_NOT_FOUND, "task not found", {"id": task_id})
        return row

    async def send_message(self, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        tenant = self._tenant(params)
        msg = params.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("parts"), list) or not msg["parts"]:
            raise RpcError(INVALID_PARAMS, "message.parts 필수")
        msg = {"messageId": msg.get("messageId") or str(uuid.uuid4()), "role": msg.get("role", "ROLE_USER"), **msg}
        cfg = params.get("configuration") or {}
        skill_id = (params.get("metadata") or {}).get("skill") or (msg.get("metadata") or {}).get("skill")

        if msg.get("taskId"):  # 기존 Task 이어가기 (INPUT_REQUIRED 등)
            row = self._require(tenant, msg["taskId"])
            if row.state in t.TERMINAL:
                raise RpcError(UNSUPPORTED_OPERATION, "종료 상태 Task 에는 메시지를 보낼 수 없음", {"state": row.state})
            skill = self.spec.skill(row.skill)
            row = self._tr(tenant, row.id, t.WORKING, append_history=msg)
        else:
            skill = self.spec.skill(skill_id)
            if skill is None:
                raise RpcError(INVALID_PARAMS, f"알 수 없는 skill {skill_id}", {"skills": [s.id for s in self.spec.skills]})
            row = self.store.create(tenant, skill.id, msg, msg.get("contextId"))
            row = self._tr(tenant, row.id, t.WORKING)

        if isinstance(cfg.get("pushNotificationConfig"), dict) and self.push is not None:   # SendMessage 인라인 webhook
            self._put_push(tenant, row.id, cfg["pushNotificationConfig"])
        run = self._run(tenant, row, skill, headers)
        if cfg.get("returnImmediately"):
            job = asyncio.create_task(run)
            self._background.add(job)
            job.add_done_callback(self._background.discard)
            return {"task": t.to_wire(row)}
        return {"task": t.to_wire(await run)}

    async def _run(self, tenant: str, row: t.TaskRow, skill: Any, headers: dict[str, str]) -> t.TaskRow:
        # OB-TRACE-04: 호출자의 traceparent 를 부모로 invoke_agent span (GenAI agent semconv)
        attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": self.spec.name,
                 "gen_ai.agent.id": self.spec.unit_id, "atlas.unit_id": self.spec.unit_id, "atlas.tenant": tenant,
                 "a2a.task.id": row.id, "a2a.context.id": row.context_id, "a2a.skill": skill.id}
        with otel.tracer().start_as_current_span(f"invoke_agent {self.spec.name}", context=otel.extract(headers),
                                                 kind=SpanKind.SERVER, attributes=attrs) as span:
            try:
                done = await self._execute(tenant, row, skill, headers)
            except t.InvalidTransition:  # 실행 중 CancelTask 로 이미 종료된 경우 — 종료 상태 유지
                done = self.store.get(tenant, row.id) or row
            span.set_attribute("a2a.task.state", done.state)
            if done.state == t.FAILED:
                span.set_status(Status(StatusCode.ERROR, "task failed"))
            return done

    async def _execute(self, tenant: str, row: t.TaskRow, skill: Any, headers: dict[str, str]) -> t.TaskRow:
        text, data = _split_parts(row.history[-1]["parts"]) if row.history else ("", {})
        ctx = TaskContext(tenant=tenant, task_id=row.id, context_id=row.context_id, skill=skill.id,
                          text=text, data=data, history=row.history, headers=headers)
        md = (row.history[-1].get("metadata") or {}) if row.history else {}
        pins = [prompts.pin(md.get("atlas.prompts")), prompts.pin(md.get("atlas.harness"), "harness")]   # 게시 게이트의 후보 버전 고정 (BL-HARN-06)
        try:
            with flags.overrides(md.get("atlas.flags")):   # ablation 평가의 요청 단위 보정 플래그 (BL-HARN-05)
                out = await skill.handler(ctx)
        except InputRequired as need:
            return self._tr(tenant, row.id, t.INPUT_REQUIRED, message=_agent_message(str(need)))
        except Exception as exc:
            log.exception("skill %s failed", skill.id)
            return self._tr(tenant, row.id, t.FAILED, message=_agent_message(f"{type(exc).__name__}: {exc}"))
        finally:
            for tok in reversed(pins):
                prompts.unpin(tok)
        part = {"data": out, "mediaType": "application/json"} if isinstance(out, dict) else {"text": str(out)}
        artifact = {"artifactId": str(uuid.uuid4()), "name": skill.id, "parts": [part]}
        return self._tr(tenant, row.id, t.COMPLETED, artifacts=[artifact])

    async def get_task(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        hl = params.get("historyLength")
        if hl is not None and (not isinstance(hl, int) or hl < 0):
            raise RpcError(INVALID_PARAMS, "historyLength 는 0 이상 정수")
        return t.to_wire(self._require(self._tenant(params), params.get("id")), hl)

    async def list_tasks(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        size = params.get("pageSize", 50)
        if not isinstance(size, int) or not 1 <= size <= 100:
            raise RpcError(INVALID_PARAMS, "pageSize 1~100")
        state = params.get("status")
        if state is not None and state not in t.STATES:
            raise RpcError(INVALID_PARAMS, f"알 수 없는 status {state}")
        token = params.get("pageToken") or "0"
        if not str(token).isdigit():
            raise RpcError(INVALID_PARAMS, "pageToken 오류")
        offset = int(token)
        rows, total = self.store.list(self._tenant(params), params.get("contextId"), state, size, offset)
        nxt = str(offset + size) if offset + size < total else ""
        return {"tasks": [t.to_wire(r, 0) for r in rows], "nextPageToken": nxt, "pageSize": size, "totalSize": total}

    async def cancel_task(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        tenant = self._tenant(params)
        row = self._require(tenant, params.get("id"))
        if row.state in t.TERMINAL:
            raise RpcError(TASK_NOT_CANCELABLE, "이미 종료된 Task", {"state": row.state})
        return t.to_wire(self._tr(tenant, row.id, t.CANCELED))

    # ---- 스트리밍 (SendStreamingMessage · SubscribeToTask) ----
    async def stream(self, body: dict[str, Any], headers: dict[str, str]):
        """SSE 로 보낼 JSON-RPC 응답들을 낸다: 첫 task 스냅샷 → statusUpdate … (final 에서 끝)."""
        rid, params = body.get("id"), body.get("params") or {}
        try:
            await self._authorize(body.get("method", ""), params, headers)
            tenant = self._tenant(params)
            if body.get("method") == "SubscribeToTask":
                row = self._require(tenant, params.get("id"))
                if row.state in t.TERMINAL:
                    raise RpcError(UNSUPPORTED_OPERATION, "종료된 Task 는 구독할 수 없음", {"state": row.state})
                q = self._subscribe(tenant, row.id)
            else:
                p2 = {**params, "configuration": {**(params.get("configuration") or {}), "returnImmediately": True}}
                first = await self.send_message(p2, headers)
                # returnImmediately 의 실행 작업은 다음 이벤트 루프 틱에 시작한다 → 여기서 구독하면 이후 전이를 놓치지 않는다
                row = self.store.get(tenant, first["task"]["id"])
                q = self._subscribe(tenant, row.id)
        except RpcError as err:
            yield error_body(rid, err)
            return
        try:
            yield {"jsonrpc": "2.0", "id": rid, "result": {"task": t.to_wire(row)}}
            if status_event(row)["statusUpdate"]["final"]:
                return
            while True:
                ev, cur = await asyncio.wait_for(q.get(), timeout=float(os.environ.get("ATLAS_A2A_STREAM_TIMEOUT_S", "300")))
                if cur.state == t.COMPLETED:
                    for a in cur.artifacts:
                        yield {"jsonrpc": "2.0", "id": rid, "result": {"artifactUpdate": {"taskId": cur.id, "contextId": cur.context_id,
                                                                                         "artifact": a, "lastChunk": True}}}
                yield {"jsonrpc": "2.0", "id": rid, "result": ev}
                if ev["statusUpdate"]["final"]:
                    return
        except TimeoutError:
            return
        finally:
            self._unsubscribe(tenant, row.id, q)

    # ---- Push 설정 CRUD (FR-A2A-SRV-03) ----
    def _put_push(self, tenant: str, task_id: str, cfg: dict[str, Any]) -> Any:
        from atlas_agent_core import push as push_mod
        if self.push is None:
            raise RpcError(UNSUPPORTED_OPERATION, "push 비활성")
        if not isinstance(cfg.get("url"), str):
            raise RpcError(INVALID_PARAMS, "pushNotificationConfig.url 필수")
        try:
            return self.push.store.put(tenant, task_id, cfg["url"], cfg.get("token"), cfg.get("id"))
        except push_mod.UnsafeURL as exc:
            raise RpcError(INVALID_PARAMS, f"webhook 거부: {exc}") from exc

    async def create_push(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        from atlas_agent_core import push as push_mod
        tenant = self._tenant(params)
        row = self._require(tenant, params.get("taskId"))
        cfg = params.get("pushNotificationConfig") or params.get("config") or {}
        return push_mod.config_wire(self._put_push(tenant, row.id, cfg))

    async def list_push(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        from atlas_agent_core import push as push_mod
        tenant = self._tenant(params)
        row = self._require(tenant, params.get("taskId") or params.get("id"))
        return {"configs": [push_mod.config_wire(r) for r in (self.push.store.list(tenant, row.id) if self.push else [])]}

    async def get_push(self, params: dict[str, Any], h: dict[str, str]) -> dict[str, Any]:
        cs = (await self.list_push(params, h))["configs"]
        hit = next((c for c in cs if c["pushNotificationConfig"]["id"] == params.get("configId")), None)
        if hit is None:
            raise RpcError(TASK_NOT_FOUND, "push 설정 없음")
        return hit

    async def delete_push(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        tenant = self._tenant(params)
        row = self._require(tenant, params.get("taskId"))
        n = self.push.store.delete(tenant, row.id, str(params.get("configId"))) if self.push else 0
        if not n:
            raise RpcError(TASK_NOT_FOUND, "push 설정 없음")
        return {}

    async def extended_card(self, params: dict[str, Any], _: dict[str, str]) -> dict[str, Any]:
        """인증된 호출자 전용 확장 카드 (FR-A2A-CARD-01) — 공개 카드 + 인증 전용 skill·운영 메타데이터."""
        return {**self.card, "metadata": {**self.card.get("metadata", {}), "atlas.extended": True,
                                          "atlas.limits": {"maxConcurrentTasks": int(os.environ.get("ATLAS_A2A_MAX_TASKS", "32"))}}}
