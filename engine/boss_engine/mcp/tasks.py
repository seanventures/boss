"""LONG-RUNNING MCP TOOLS AS TASKS, AND A PERSON'S ANSWER AS `input_required` (AP-P5,
BOSSAPP-1056; the `io.modelcontextprotocol/tasks` extension, SEP-2663; MRTR, SEP-2322).

A task IS a BOSS run — nothing new is stored. The task id is a server-minted HANDLE naming the
run (a deployed agent's run on its thread, or a code task run), passed back by the client as an
ordinary argument (2026-07-28: "cross-call state uses server-minted handles"). It is opaque to
the client and carries no authority: every `tasks/get`, `tasks/update` and `tasks/cancel` is
decided live as the caller of THAT request (agent:invoke for a deployed agent's run,
code_task:read for a build, decision:approve with step-up for an answer), so a guessed or leaked
handle reaches nothing its holder could not already read.

The run's status maps onto the extension's lifecycle:

    pending / running / resuming          working
    waiting on a pending Decision         input_required  (one elicitation per interrupt)
    success                               completed       (`result` = the tools/call result)
    error                                 failed          (`error` = a JSON-RPC error)
    cancelled / rolled back               cancelled

Two backends answer the same five questions: NATIVE (the server holds the engine: it reads the
database and decides with the PDP) and API (the server is a bridge: every read, cancel and
answer is the API route itself, called with the caller's own credential — M8).
"""
from __future__ import annotations

import base64
import json
from typing import Any, Mapping, Optional

from . import protocol as P

AGENT_RUN = "agent_run"
CODE_TASK = "code_task"
#: The tools whose MCP call becomes a task when the client opted in to the extension.
TASK_TOOLS = frozenset({"create_code_task"})
AGENT_PREFIX = "agent_"


class TaskError(Exception):
    def __init__(self, code: int, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def error(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out


# ── handles ────────────────────────────────────────────────────────────────────────────────────

def make_handle(kind: str, ref: str, *, thread_id: Optional[str] = None) -> str:
    body = {"k": kind, "r": ref, **({"t": thread_id} if thread_id else {})}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return "bt1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_handle(task_id: Any) -> dict:
    if not isinstance(task_id, str) or not task_id.startswith("bt1."):
        raise TaskError(P.TASK_NOT_FOUND, "unknown task")
    raw = task_id[4:]
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        raise TaskError(P.TASK_NOT_FOUND, "unknown task") from None
    if not isinstance(body, dict) or body.get("k") not in (AGENT_RUN, CODE_TASK) \
            or not isinstance(body.get("r"), str):
        raise TaskError(P.TASK_NOT_FOUND, "unknown task")
    return body


def is_task_tool(name: str) -> bool:
    return name in TASK_TOOLS or name.startswith(AGENT_PREFIX)


def handle_for_tool_result(name: str, result: Mapping[str, Any]) -> Optional[str]:
    """The task a long-running tool's start result names, or None (the call did not start one —
    a refusal, an error: answered as an ordinary result)."""
    if not isinstance(result, Mapping) or result.get("ok") is False:
        return None
    if name.startswith(AGENT_PREFIX):
        body = result.get("result") if isinstance(result.get("result"), Mapping) else result
        run_id, thread_id = body.get("run_id"), body.get("thread_id")
        return make_handle(AGENT_RUN, str(run_id), thread_id=str(thread_id)) \
            if run_id and thread_id else None
    if name == "create_code_task":
        body = result.get("result") if isinstance(result.get("result"), Mapping) else result
        run_id = body.get("run_id")
        return make_handle(CODE_TASK, str(run_id)) if run_id else None
    return None


# ── the task object ────────────────────────────────────────────────────────────────────────────

def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        text = value.isoformat()
    else:
        text = str(value or "")
    return text.replace("+00:00", "Z")


def task_object(task_id: str, state: Mapping[str, Any], *, elicit: set) -> dict:
    """The extension's DetailedTask for a run's state (see the module docstring's table)."""
    status = state["status"]
    out = {"taskId": task_id, "status": status, "createdAt": _iso(state.get("created_at")),
           "lastUpdatedAt": _iso(state.get("updated_at") or state.get("created_at")),
           "ttlMs": None, "pollIntervalMs": P.POLL_INTERVAL_MS}
    if state.get("message"):
        out["statusMessage"] = str(state["message"])[:500]
    if status == "input_required":
        out["inputRequests"] = input_requests(state.get("pending") or [], elicit=elicit,
                                              refused=state.get("refused") or {})
    elif status == "completed":
        out["result"] = state.get("result") or {"content": [], "isError": False}
    elif status == "failed":
        out["error"] = {"code": -32603, "message": str(state.get("error") or "the run failed")}
    return out


def input_requests(pending: list, *, elicit: set, refused: Optional[Mapping] = None) -> dict:
    """One elicitation per open interrupt: a form the client fills in, or — when the caller
    cannot answer it in-band (it was refused for step-up), or the client renders only URLs — a
    URL-mode request to the console, where the person approves with a fresh second factor."""
    from ..services import protocol_answers as A

    out = {}
    for item in pending:
        key = A.input_key(item["decision_id"])
        why = (refused or {}).get(item["decision_id"])
        if (why or "form" not in elicit) and "url" in elicit:
            out[key] = A.url_elicitation(item, reason=str(why or ""))
        else:
            out[key] = A.form_elicitation(item)
    return out


def tool_result(payload: Mapping[str, Any], *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "structuredContent": json.loads(json.dumps(payload, default=str)),
            "isError": bool(is_error)}


def _agent_state(run: Mapping[str, Any], pending: list) -> dict:
    status = str(run.get("status") or "")
    base = {"created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
            "pending": pending}
    if status in ("pending", "running"):
        return {**base, "status": "working"}
    if status == "interrupted":
        if pending:
            return {**base, "status": "input_required",
                    "message": "waiting on a person: " + "; ".join(
                        str(p.get("question") or "")[:120] for p in pending)}
        return {**base, "status": "working", "message": "answered; resuming"}
    if status == "success":
        return {**base, "status": "completed",
                "result": tool_result({"ok": True, **{k: v for k, v in run.items()
                                                      if k != "interrupts"}})}
    if status == "error":
        return {**base, "status": "failed", "error": run.get("error") or "the run failed"}
    return {**base, "status": "cancelled"}


def _code_task_state(run: Mapping[str, Any], pending: list) -> dict:
    status = str(run.get("status") or "")
    base = {"created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
            "pending": pending}
    if status == "completed":
        keep = {k: run.get(k) for k in ("id", "status", "phase", "title", "pull_request_url")}
        return {**base, "status": "completed", "result": tool_result({"ok": True, "run": keep})}
    if status == "failed":
        state = run.get("state") if isinstance(run.get("state"), Mapping) else {}
        return {**base, "status": "failed",
                "error": run.get("error") or state.get("error") or "the build failed"}
    if status in ("cancelled", "canceled"):
        return {**base, "status": "cancelled"}
    if pending:
        return {**base, "status": "input_required",
                "message": "waiting on a person: " + "; ".join(
                    str(p.get("question") or "")[:120] for p in pending)}
    return {**base, "status": "working", "message": str(run.get("phase") or "")}


# ── backends ───────────────────────────────────────────────────────────────────────────────────

def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


class NativeBackend:
    """The server holds the engine: reads the database, decides with the PDP as the caller."""

    def __init__(self, server: Any):
        self.server = server

    def _engine(self) -> Any:
        engine = getattr(self.server.ctx, "engine", None)
        if engine is None:
            raise TaskError(-32603, "durable workspace required")
        return engine

    def _permit(self, connection: Any, auth: Any, permission: str) -> None:
        from ..identity import authz
        from ..identity.enforcement import AuthzMode
        from ..identity.scope import ResolvedScope
        from .server import _mcp_mode

        mode = _mcp_mode()
        if mode == AuthzMode.OFF:
            return
        if auth is None:
            if mode == AuthzMode.ENFORCE:
                raise TaskError(-32001, "identity required: present your BOSS credential")
            return
        verdict = authz.check(auth, permission, ResolvedScope.in_project(
            auth.principal.org_id, self.server.project_id), db=connection)
        if not verdict.allowed and mode == AuthzMode.ENFORCE:
            # Never tell a caller who may not read it that the run exists.
            raise TaskError(P.TASK_NOT_FOUND, "unknown task")

    def state(self, handle: dict, auth: Any) -> dict:
        from ..db import begin
        from ..services import agent_protocol
        from ..services import protocol_answers as A
        from sqlalchemy import text

        with begin(self._engine()) as c:
            if handle["k"] == AGENT_RUN:
                self._permit(c, auth, "agent:invoke")
                try:
                    run = agent_protocol.get_run(c, self.server.project_id, handle["r"],
                                                 handle.get("t"))
                except agent_protocol.ProtocolError:
                    raise TaskError(P.TASK_NOT_FOUND, "unknown task") from None
                return _agent_state(run, A.pending_for(c, handle["r"]))
            self._permit(c, auth, "code_task:read")
            row = c.execute(text(
                "SELECT id, status, phase, title, pull_request_url, state, created_at, "
                "updated_at FROM code_task_runs WHERE id = :r AND project_id = :p"),
                {"r": handle["r"], "p": self.server.project_id}).mappings().first()
            if row is None:
                raise TaskError(P.TASK_NOT_FOUND, "unknown task")
            run = {**dict(row), "state": _json(row["state"], {})}
            return _code_task_state(run, A.pending_for(c, handle["r"]))

    def cancel(self, handle: dict, auth: Any) -> None:
        from ..db import begin
        from ..services import agent_protocol

        if handle["k"] == AGENT_RUN:
            with begin(self._engine()) as c:
                self._permit(c, auth, "agent:invoke")
                try:
                    agent_protocol.cancel_run(c, project_id=self.server.project_id,
                                              run_id=handle["r"],
                                              actor=_actor(auth, self.server.actor))
                except agent_protocol.ProtocolError:
                    raise TaskError(P.TASK_NOT_FOUND, "unknown task") from None
            return
        # A build is cancelled by its own tool, under the registry's identity gate.
        self.server._invoke_tool("cancel_code_task", {"run_id": handle["r"]}, auth)

    def answer(self, decision_id: str, verdict: dict, auth: Any) -> dict:
        from ..db import begin
        from ..services import protocol_answers as A

        with begin(self._engine()) as c:
            return A.answer(c, auth, project_id=self.server.project_id,
                            decision_id=decision_id, verdict=verdict["verdict"],
                            note=verdict.get("note") or "", answer_value=verdict.get("answer"),
                            via="mcp")


class ApiBackend:
    """The server bridges to the API: every read, cancel and answer IS the API route, called with
    the caller's own credential (M8), so the route's PDP — step-up included — decides."""

    def __init__(self, server: Any):
        self.server = server
        self.surface = server.api_surface

    def _call(self, method: str, path: str, *, path_args: Optional[dict] = None,
              query: Optional[dict] = None, body: Any = None) -> dict:
        from .api_surface import operation_alias

        out = self.surface.invoke(operation_alias(method, path),
                                  project_id=self.server.project_id, path=path_args or {},
                                  query=query or None, body=body)
        return out if isinstance(out, dict) else {"ok": False}

    def state(self, handle: dict, auth: Any) -> dict:
        if handle["k"] == AGENT_RUN:
            got = self._call("GET", "/v1/threads/{thread_id}/runs/{run_id}",
                             path_args={"thread_id": handle.get("t"), "run_id": handle["r"]})
            if not got.get("ok"):
                raise TaskError(P.TASK_NOT_FOUND, "unknown task")
            run = got.get("result") or {}
            pending = [{"decision_id": d.get("decision_id"), "question": d.get("question"),
                        "kind": "approval"}
                       for d in run.get("interrupts") or [] if d.get("status") == "pending"]
            return _agent_state(run, pending)
        got = self._call("GET", "/v1/code-tasks/{run_id}", path_args={"run_id": handle["r"]})
        if not got.get("ok"):
            raise TaskError(P.TASK_NOT_FOUND, "unknown task")
        run = got.get("result") or {}
        run = run.get("run") if isinstance(run.get("run"), Mapping) else run
        pending = []
        state = run.get("state") if isinstance(run.get("state"), Mapping) else {}
        if state.get("awaiting_plan_approval") and run.get("decision_id"):
            pending = [{"decision_id": run["decision_id"], "question": "Approve the plan?",
                        "kind": "plan"}]
        return _code_task_state(run, pending)

    def cancel(self, handle: dict, auth: Any) -> None:
        if handle["k"] == AGENT_RUN:
            got = self._call("POST", "/v1/threads/{thread_id}/runs/{run_id}/cancel",
                             path_args={"thread_id": handle.get("t"), "run_id": handle["r"]},
                             body={})
        else:
            got = self._call("POST", "/v1/code-tasks/{run_id}/cancel",
                             path_args={"run_id": handle["r"]}, body={})
        if not got.get("ok") and got.get("status") == 404:
            raise TaskError(P.TASK_NOT_FOUND, "unknown task")

    def answer(self, decision_id: str, verdict: dict, auth: Any) -> dict:
        from ..services.protocol_answers import AnswerRefused

        got = self._call("POST", f"/v1/decisions/{{decision_id}}/{verdict['verdict']}",
                         path_args={"decision_id": decision_id},
                         body={"note": verdict.get("note") or "answered over MCP",
                               **({"answer": verdict["answer"]} if verdict.get("answer") else {})})
        if got.get("ok"):
            return {"decision_id": decision_id,
                    "status": (got.get("result") or {}).get("status") or verdict["verdict"]}
        result = got.get("result") if isinstance(got.get("result"), Mapping) else {}
        detail = result.get("detail") if isinstance(result.get("detail"), Mapping) else result
        code = str(detail.get("error") or got.get("error") or "refused")
        raise AnswerRefused(code, str(detail.get("message") or code), decision_id=decision_id)


def _actor(auth: Any, fallback: str) -> str:
    return str(getattr(getattr(auth, "principal", None), "id", "") or fallback)


def backend_for(server: Any) -> Any:
    return ApiBackend(server) if getattr(server, "api_only", False) else NativeBackend(server)


__all__ = ["AGENT_RUN", "ApiBackend", "CODE_TASK", "NativeBackend", "TASK_TOOLS", "TaskError",
           "backend_for", "handle_for_tool_result", "input_requests", "is_task_tool",
           "make_handle", "parse_handle", "task_object", "tool_result"]
