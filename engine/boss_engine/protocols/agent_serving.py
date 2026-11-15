"""THE SHARED AGENT PROTOCOL + A2A SERVING LIBRARY (P8b-9, BOSSAPP-1076; boss-agent-hosts-lld.md
§3.13.1 "the same routes and schemas … from a shared library (FR-22)", §3.13.2).

One set of handlers, two backends:

  * IN-PROCESS (`InProcessBackend`): the control plane's own database and services — what the API
    serves for `runtime: local`, and what the tests drive to prove parity;
  * IN-VM (`runtime/serving.RunPlaneBackend`): the replica's boss-runtime behind its TLS
    listener on VIP:443. Every request is authorised by BOSS (`POST /v1/run-plane/admit`, which
    re-runs the PDP `agent:invoke` with the forwarded caller credential); runs execute in the VM
    and stream back on the same connection.

Framework-free: a `ServingRequest` (method, path, headers, body) in, a `ServingResponse` (status
and a JSON body, or an iterator of SSE frames) out. The routes, the same paths and shapes as
apps/api/app/routers/agent_platform.py and routers/a2a.py (a `/v1` prefix is optional, the
project is implicit — one agent per listener):

    POST /threads                                  create a thread               201
    GET  /threads/{t}                              (not served in the VM: 404)
    POST /threads/{t}/runs                         admit a run                   201
    POST /threads/{t}/runs/stream                  admit + SSE (metadata, updates, messages,
                                                   custom, values, end)
    POST /threads/{t}/runs/wait                    admit + wait for the end or a person
    POST /agents/{key}/runs/stream | /runs/wait    the same on a new thread (stateless)
    POST /runs/stream | /runs/wait                 the same, agent implicit
    GET  /threads/{t}/runs/{r}                     the run
    GET  /threads/{t}/runs/{r}/stream              join a run's stream
    POST /threads/{t}/runs/{r}/cancel              cancel
    POST /a2a  |  /a2a/{agent_id}                  A2A v1.0 JSON-RPC (SendMessage,
                                                   SendStreamingMessage → SSE, GetTask, CancelTask)
    GET  /.well-known/agent-card.json  (and /a2a/{id}/.well-known/agent-card.json)  the signed card

The typed SSE events of a run (`checkpoint_events`, `final_events`) are built here and used by
services/agent_protocol.stream too, so a stream reads the same in-process and in a VM.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol

from . import a2a_types as t

TERMINAL = frozenset({"success", "error", "cancelled"})
BLOCKING_WAIT_SECONDS = 60.0
STREAM_SECONDS = 600.0
MAX_BODY_BYTES = 1 << 20


# ── Requests, responses, callers ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Caller:
    """The caller's credential as the listener received it. The listener verified it locally
    (signature / chain) and FORWARDS it: authorisation is BOSS's, never the listener's."""
    authorization: Optional[str] = None
    client_cert_pem: Optional[str] = None
    traceparent: Optional[str] = None

    def __repr__(self) -> str:  # never print a credential
        return (f"Caller(bearer={'yes' if self.authorization else 'no'}, "
                f"cert={'yes' if self.client_cert_pem else 'no'})")


@dataclass
class ServingRequest:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    query: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


@dataclass
class ServingResponse:
    status: int = 200
    body: Any = None
    stream: Optional[Iterator[str]] = None
    headers: dict = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        return "text/event-stream" if self.stream is not None else "application/json"


class ServingError(Exception):
    """A refusal a backend raises (authn, authz, not found, closed). `status` is the HTTP one."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code

    def body(self) -> dict:
        return {"detail": {"error": self.code, "message": self.message}}


class Backend(Protocol):
    """What the handlers need. `surface` is "agent_protocol" or "a2a" (A2A also runs D11)."""

    def card(self, caller: Caller) -> Optional[dict]: ...

    def create_thread(self, caller: Caller, *, metadata: Optional[dict]) -> dict: ...

    def start_run(self, caller: Caller, *, thread_id: Optional[str], input: Any,
                  double_text: Optional[str], surface: str) -> dict: ...

    def view(self, caller: Caller, run_id: str, *, surface: str) -> dict: ...

    def cancel(self, caller: Caller, run_id: str, *, surface: str) -> dict: ...

    def answer(self, caller: Caller, run_id: str, answers: list, *, surface: str) -> None: ...

    def events(self, caller: Caller, run_id: str, *, timeout: float,
               surface: str) -> Iterator[dict]: ...


# ── The typed run events (shared with services/agent_protocol.stream) ──────────────────────

def checkpoint_events(cp: Mapping[str, Any], state: Mapping[str, Any],
                      seen_messages: int) -> tuple:
    """One checkpoint's events: an `updates` and a `messages` per new message. Returns
    (events, new seen_messages count)."""
    out = [{"type": "updates", "ns": [f"step:{cp.get('step')}"],
            "data": {"checkpoint_id": cp.get("checkpoint_id"), "step": cp.get("step"),
                     "phase": cp.get("phase"), "status": cp.get("status"),
                     "source": cp.get("source")}}]
    messages = list((state or {}).get("messages") or [])
    for message in messages[seen_messages:]:
        out.append({"type": "messages", "ns": [f"step:{cp.get('step')}"],
                    "data": {"role": message.get("role"), "content": message.get("content"),
                             "tool_calls": message.get("tool_calls"),
                             "tool_call_id": message.get("tool_call_id")}})
    return out, max(seen_messages, len(messages))


def waits_on_a_person(run: Mapping[str, Any]) -> bool:
    return run.get("status") == "interrupted" and any(
        d.get("status") == "pending" for d in run.get("interrupts") or [])


def interrupt_events(run: Mapping[str, Any], announced: set) -> list:
    if run.get("status") != "interrupted":
        return []
    fresh = [d for d in run.get("interrupts") or [] if d.get("status") == "pending"
             and d.get("decision_id") not in announced]
    if not fresh:
        return []
    announced.update(d["decision_id"] for d in fresh)
    return [{"type": "custom", "ns": [], "data": {
        "kind": "interrupt", "decisions": fresh,
        "how": "Answer each through the Decision API (POST /v1/decisions/{id}/"
               "approve or /reject); the run resumes when the last is answered."}}]


def final_events(run: Mapping[str, Any], *, timed_out: bool = False) -> list:
    end = {"status": run.get("status")}
    if timed_out:
        end["timed_out"] = True
    return [{"type": "values", "ns": [], "data": dict(run)},
            {"type": "end", "ns": [], "data": end}]


def metadata_event(run: Mapping[str, Any]) -> dict:
    return {"type": "metadata", "ns": [], "data": {
        "run_id": run.get("run_id"), "thread_id": run.get("thread_id"),
        "agent_key": run.get("agent_key"), "version": run.get("version")}}


def stopped(run: Mapping[str, Any], *, stop_on_interrupt: bool = True) -> bool:
    return run.get("status") in TERMINAL or (stop_on_interrupt and waits_on_a_person(run))


def sse(event: Mapping[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


def sse_data(payload: Any) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# ── A2A: a run's view as a Task (shared with services/a2a_server.task_for_run) ─────────────

def task_from_view(view: Mapping[str, Any], *, history_length: Optional[int] = None) -> t.Task:
    """`view` = {"run": agent_protocol.get_run(...), "pending": [{decision_id, question, kind,
    approve_url}], "inputs": [user message text, ...]}."""
    run = view["run"]
    run_id, thread_id = str(run["run_id"]), run.get("thread_id")
    pending = list(view.get("pending") or [])
    status = run["status"]
    state = (t.TaskState.WORKING if status == "interrupted" and not pending
             else t.state_for_run_status(status))
    message = None
    artifacts: list = []
    if state is t.TaskState.INPUT_REQUIRED:
        message = t.agent_message(
            "Waiting on a person: " + "; ".join(str(p.get("question") or "")[:200]
                                                 for p in pending),
            context_id=thread_id, task_id=run_id,
            data={"decisions": [{"decision_id": p["decision_id"], "question": p.get("question"),
                                 "kind": p.get("kind"), "approve_url": p.get("approve_url")}
                                for p in pending],
                  "how": "A person approves each decision in BOSS (with a fresh sign-in check); "
                         "then send a message on this task to resume, or wait for the push "
                         "notification."})
    elif state is t.TaskState.COMPLETED:
        output = str(run.get("output") or "")
        artifacts = [t.Artifact(parts=[t.text_part(output)], name="output",
                                artifact_id=f"art_{run_id}")]
        message = t.agent_message(output, context_id=thread_id, task_id=run_id)
    elif state is t.TaskState.FAILED and run.get("error"):
        message = t.agent_message(str(run["error"])[:2000], context_id=thread_id, task_id=run_id)
    history = [t.Message(role=t.Role.USER, parts=[t.text_part(str(text_))],
                         message_id=f"msg_{run_id}_in", context_id=thread_id, task_id=run_id)
               for text_ in view.get("inputs") or [] if text_]
    stamp = str(run.get("updated_at") or "").replace("+00:00", "Z") or None
    return t.Task(id=run_id, context_id=thread_id,
                  status=t.TaskStatus(state, message, stamp) if stamp else t.TaskStatus(state,
                                                                                       message),
                  artifacts=artifacts, history=history[-(history_length or 50):]
                  if history_length != 0 else [],
                  metadata={"agent_key": run.get("agent_key"), "version": run.get("version")})


class A2AError(Exception):
    def __init__(self, code: int, message: str, *, data: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def error(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out


def _result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str, data: Optional[dict] = None) -> dict:
    out: dict = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    if data:
        out["error"]["data"] = data
    return out


def _a2a_from_serving(exc: ServingError) -> A2AError:
    if exc.status == 404:
        return A2AError(t.TASK_NOT_FOUND, exc.message)
    if exc.status in (401, 403):
        return A2AError(-32600 if exc.status == 401 else -32603, f"{exc.code}: {exc.message}")
    return A2AError(-32603 if exc.status >= 500 else -32602, exc.message)


def check_version(header: Optional[str]) -> None:
    value = str(header or "").strip()
    if value and ".".join(value.split(".")[:2]) not in ("1.0", "0.3"):
        raise A2AError(t.VERSION_NOT_SUPPORTED, f"A2A version {value} is not supported",
                       data={"supported": ["1.0", "0.3"]})


def _wait(backend: Backend, caller: Caller, run_id: str, seconds: float) -> None:
    for event in backend.events(caller, run_id, timeout=seconds, surface="a2a"):
        if event.get("type") == "end":
            return


def _send(backend: Backend, caller: Caller, req: t.SendMessageRequest) -> str:
    msg = req.message
    if msg.task_id:
        view = backend.view(caller, msg.task_id, surface="a2a")
        if task_from_view(view).state.terminal:
            raise A2AError(t.UNSUPPORTED_OPERATION,
                           "this task has ended; send a new message on its contextId instead")
        answers = [d for d in msg.data() if isinstance(d, Mapping) and d.get("decision_id")]
        if answers:
            backend.answer(caller, msg.task_id, answers, surface="a2a")
        return str(msg.task_id)
    content = msg.text() or (json.dumps(msg.data()[0], default=str) if msg.data() else "")
    if not content.strip():
        raise A2AError(-32602, "the message carries no text or data")
    run = backend.start_run(caller, thread_id=msg.context_id, input=content, double_text=None,
                            surface="a2a")
    return str(run["run_id"])


def a2a_handle(backend: Backend, caller: Caller, request: Any) -> dict:
    """One non-streaming JSON-RPC request."""
    rid = request.get("id") if isinstance(request, Mapping) else None
    try:
        if not isinstance(request, Mapping) or request.get("jsonrpc") != "2.0":
            raise A2AError(-32600, "invalid JSON-RPC request")
        method = t.method_name(str(request.get("method") or ""))
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            raise A2AError(-32602, "params must be an object")
        if method == "SendMessage":
            req = t.SendMessageRequest.from_dict(params)
            run_id = _send(backend, caller, req)
            if not req.configuration.return_immediately:
                _wait(backend, caller, run_id, BLOCKING_WAIT_SECONDS)
            task = task_from_view(backend.view(caller, run_id, surface="a2a"),
                                  history_length=req.configuration.history_length)
            return _result(rid, {"task": task.to_dict()})
        if method == "GetTask":
            task = task_from_view(backend.view(caller, str(params.get("id") or ""),
                                               surface="a2a"),
                                  history_length=params.get("historyLength"))
            return _result(rid, task.to_dict())
        if method == "CancelTask":
            task_id = str(params.get("id") or "")
            if task_from_view(backend.view(caller, task_id, surface="a2a")).state.terminal:
                raise A2AError(t.TASK_NOT_CANCELABLE, "the task has already ended")
            backend.cancel(caller, task_id, surface="a2a")
            return _result(rid, task_from_view(backend.view(caller, task_id,
                                                            surface="a2a")).to_dict())
        if method == "GetExtendedAgentCard":
            raise A2AError(t.EXTENDED_AGENT_CARD_NOT_CONFIGURED, "no extended card")
        if method in t.METHODS:
            raise A2AError(t.UNSUPPORTED_OPERATION, f"{method} is not supported here")
        raise A2AError(-32601, f"method not found: {method}")
    except ServingError as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": _a2a_from_serving(exc).error()}
    except A2AError as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
    except t.A2AShapeError as exc:
        return _rpc_error(rid, -32602, str(exc))


def a2a_stream(backend: Backend, caller: Caller, request: Mapping[str, Any]) -> Iterator[dict]:
    """SendStreamingMessage: the Task first, then a statusUpdate per state change and the output
    artifact; it closes when the task ends or stops for a person."""
    rid = request.get("id")
    try:
        req = t.SendMessageRequest.from_dict(request.get("params") or {})
        run_id = _send(backend, caller, req)
        task = task_from_view(backend.view(caller, run_id, surface="a2a"))
    except ServingError as exc:
        yield {"jsonrpc": "2.0", "id": rid, "error": _a2a_from_serving(exc).error()}
        return
    except A2AError as exc:
        yield {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
        return
    except t.A2AShapeError as exc:
        yield _rpc_error(rid, -32602, str(exc))
        return
    yield _result(rid, t.stream_response(task))
    last = task.state
    if last.terminal or last.interrupted:
        return
    for event in backend.events(caller, run_id, timeout=STREAM_SECONDS, surface="a2a"):
        if event.get("type") not in ("updates", "custom", "end"):
            continue
        task = task_from_view(backend.view(caller, run_id, surface="a2a"))
        if task.state is not last:
            last = task.state
            if task.artifacts:
                yield _result(rid, t.stream_response(t.TaskArtifactUpdateEvent(
                    task.id, str(task.context_id), task.artifacts[0], last_chunk=True)))
            yield _result(rid, t.stream_response(t.TaskStatusUpdateEvent(
                task.id, str(task.context_id), task.status)))
        if last.terminal or last.interrupted or event.get("type") == "end":
            return


# ── HTTP dispatch ──────────────────────────────────────────────────────────────────────────

_ROUTES = [
    ("POST", re.compile(r"^/(?:agents/[^/]+/)?threads$"), "create_thread"),
    ("POST", re.compile(r"^/threads/(?P<t>[^/]+)/runs$"), "start"),
    ("POST", re.compile(r"^/threads/(?P<t>[^/]+)/runs/stream$"), "start_stream"),
    ("POST", re.compile(r"^/threads/(?P<t>[^/]+)/runs/wait$"), "start_wait"),
    ("POST", re.compile(r"^/(?:agents/[^/]+/)?runs/stream$"), "stateless_stream"),
    ("POST", re.compile(r"^/(?:agents/[^/]+/)?runs/wait$"), "stateless_wait"),
    ("GET", re.compile(r"^/threads/(?P<t>[^/]+)/runs/(?P<r>[^/]+)$"), "get_run"),
    ("GET", re.compile(r"^/threads/(?P<t>[^/]+)/runs/(?P<r>[^/]+)/stream$"), "join_stream"),
    ("POST", re.compile(r"^/threads/(?P<t>[^/]+)/runs/(?P<r>[^/]+)/cancel$"), "cancel"),
    ("POST", re.compile(r"^/a2a(?:/[^/]+)?$"), "a2a"),
    ("GET", re.compile(r"^(?:/a2a/[^/]+)?/\.well-known/agent-card\.json$"), "card"),
]


def _normal(path: str) -> str:
    path = "/" + path.split("?", 1)[0].strip("/")
    return path[3:] or "/" if path.startswith("/v1/") or path == "/v1" else path


def match(method: str, path: str) -> tuple:
    p = _normal(path)
    for m, pattern, name in _ROUTES:
        found = pattern.match(p)
        if found and m == method.upper():
            return name, found.groupdict()
    return None, {}


def _json_body(req: ServingRequest) -> dict:
    if len(req.body or b"") > MAX_BODY_BYTES:
        raise ServingError(413, "body_too_large")
    try:
        body = req.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServingError(400, "invalid_json") from exc
    if not isinstance(body, dict):
        raise ServingError(400, "body_must_be_an_object")
    return body


def _timeout(body: Mapping[str, Any], default: float, cap: float = 3600.0) -> float:
    try:
        return max(0.0, min(float(body.get("timeout", default)), cap))
    except (TypeError, ValueError):
        return default


def _run_view(backend: Backend, caller: Caller, run_id: str, thread_id: Optional[str]) -> dict:
    run = backend.view(caller, run_id, surface="agent_protocol")["run"]
    if thread_id and run.get("thread_id") != thread_id:
        raise ServingError(404, "not_found", "no such run in this thread")
    return run


def _ap_stream(backend: Backend, caller: Caller, run_id: str, timeout: float) -> ServingResponse:
    events = backend.events(caller, run_id, timeout=timeout, surface="agent_protocol")
    return ServingResponse(200, stream=(sse(e) for e in events),
                           headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _join(backend: Backend, caller: Caller, run_id: str, timeout: float) -> dict:
    timed_out = True
    for event in backend.events(caller, run_id, timeout=timeout, surface="agent_protocol"):
        if event.get("type") == "end":
            timed_out = bool(event.get("data", {}).get("timed_out"))
    run = backend.view(caller, run_id, surface="agent_protocol")["run"]
    return {**run, "timed_out": timed_out and not stopped(run)}


def dispatch(backend: Backend, req: ServingRequest, caller: Caller) -> ServingResponse:
    """One request through the shared handlers. Never raises for a refusal."""
    name, args = match(req.method, req.path)
    try:
        if name is None:
            raise ServingError(404, "not_found", "no such route")
        if name == "card":
            card = backend.card(caller)
            if not card:
                raise ServingError(404, "not_found", "this agent publishes no A2A card")
            return ServingResponse(200, card, headers={"Cache-Control": "public, max-age=300"})
        if name == "a2a":
            try:
                check_version(req.headers.get("a2a-version") or req.query.get("A2A-Version"))
                body = json.loads(req.body or b"null")
            except A2AError as exc:
                return ServingResponse(200, {"jsonrpc": "2.0", "id": None, "error": exc.error()})
            except ValueError:
                return ServingResponse(200, _rpc_error(None, -32700, "parse error"))
            method = t.method_name(str((body or {}).get("method") or "")) \
                if isinstance(body, dict) else ""
            if method == "SendStreamingMessage":
                return ServingResponse(200, stream=(sse_data(x) for x in a2a_stream(
                    backend, caller, body)), headers={"Cache-Control": "no-cache"})
            return ServingResponse(200, a2a_handle(backend, caller, body))
        body = _json_body(req) if req.method.upper() == "POST" else dict(req.query)
        if name == "create_thread":
            return ServingResponse(201, backend.create_thread(
                caller, metadata=body.get("metadata") if isinstance(body.get("metadata"),
                                                                     dict) else None))
        if name in ("start", "start_stream", "start_wait", "stateless_stream", "stateless_wait"):
            run = backend.start_run(caller, thread_id=args.get("t"), input=body.get("input"),
                                    double_text=body.get("double_text"),
                                    surface="agent_protocol")
            if name == "start":
                return ServingResponse(201, run)
            if name.endswith("_stream"):
                return _ap_stream(backend, caller, str(run["run_id"]),
                                  _timeout(body, 300.0))
            return ServingResponse(200, _join(backend, caller, str(run["run_id"]),
                                              _timeout(body, 300.0, 600.0)))
        if name == "get_run":
            return ServingResponse(200, _run_view(backend, caller, args["r"], args["t"]))
        if name == "join_stream":
            _run_view(backend, caller, args["r"], args["t"])
            return _ap_stream(backend, caller, args["r"], _timeout(body, 300.0))
        if name == "cancel":
            _run_view(backend, caller, args["r"], args["t"])
            return ServingResponse(200, backend.cancel(caller, args["r"],
                                                       surface="agent_protocol"))
        raise ServingError(404, "not_found")
    except ServingError as exc:
        return ServingResponse(exc.status, exc.body())


# ── The in-process backend (the control plane's own database) ──────────────────────────────

class InProcessBackend:
    """The shared handlers over the control plane's database, for one deployed agent. `authorize`
    turns a Caller into an AuthContext and checks `agent:invoke` (the API passes the context its
    guard already built). Runs execute on the local workers (`runtime: local`)."""

    def __init__(self, engine: Any, agent: Mapping[str, Any], *,
                 authorize: Callable[[Caller], Any], base_url: str = ""):
        self.engine, self.agent, self.authorize, self.base_url = engine, dict(agent), authorize, base_url

    def _ctx(self, caller: Caller, surface: str) -> Any:
        ctx = self.authorize(caller)
        if surface == "a2a":
            from ..db import begin
            from ..services import a2a_server

            with begin(self.engine) as c:
                why = a2a_server.exposure_refusal(c, self.agent)
            if why:
                raise ServingError(404, "a2a_closed", why)
        return ctx

    def _actor(self, ctx: Any) -> str:
        return str(getattr(getattr(ctx, "principal", None), "id", "") or "api")

    def card(self, caller: Caller) -> Optional[dict]:
        from ..db import begin
        from ..services import a2a_server

        with begin(self.engine) as c:
            if a2a_server.exposure_refusal(c, self.agent):
                return None
            return a2a_server.agent_card(c, self.agent, base_url=self.base_url)

    def create_thread(self, caller: Caller, *, metadata: Optional[dict]) -> dict:
        from ..db import begin
        from ..services import agent_protocol

        ctx = self._ctx(caller, "agent_protocol")
        with begin(self.engine) as c:
            return agent_protocol.create_thread(
                c, project_id=self.agent["project_id"], agent_key=self.agent["agent_key"],
                actor=self._actor(ctx), environment=self.agent["deployment"].get("environment"),
                metadata=metadata)

    def start_run(self, caller: Caller, *, thread_id: Optional[str], input: Any,
                  double_text: Optional[str], surface: str) -> dict:
        from ..db import begin
        from ..identity import lineage
        from ..services import agent_protocol

        import contextlib

        ctx = self._ctx(caller, surface)
        actor = self._actor(ctx)
        acting = lineage.acting(ctx) if ctx is not None else contextlib.nullcontext()
        with acting, begin(self.engine) as c:
            try:
                if thread_id:
                    thread = agent_protocol.get_thread(c, self.agent["project_id"], thread_id)
                    if thread.get("agent_key") not in (None, self.agent["agent_key"]):
                        raise ServingError(404, "not_found", "that thread is another agent's")
                else:
                    thread_id = agent_protocol.create_thread(
                        c, project_id=self.agent["project_id"],
                        agent_key=self.agent["agent_key"], actor=actor,
                        environment=self.agent["deployment"].get("environment"),
                        metadata={"surface": surface})["thread_id"]
                return agent_protocol.start_run(
                    c, project_id=self.agent["project_id"], thread_id=thread_id, actor=actor,
                    input=input, double_text=double_text, traceparent=caller.traceparent,
                    trigger="a2a" if surface == "a2a" else "api")
            except agent_protocol.ProtocolError as exc:
                raise ServingError(exc.status, "refused", str(exc)) from exc

    def view(self, caller: Caller, run_id: str, *, surface: str) -> dict:
        from ..db import begin
        from ..services import a2a_server

        self._ctx(caller, surface)
        with begin(self.engine) as c:
            return a2a_server.view_for_run(c, self.agent, run_id)

    def cancel(self, caller: Caller, run_id: str, *, surface: str) -> dict:
        from ..db import begin
        from ..services import a2a_server, agent_protocol

        ctx = self._ctx(caller, surface)
        with begin(self.engine) as c:
            a2a_server.view_for_run(c, self.agent, run_id)
            return agent_protocol.cancel_run(c, project_id=self.agent["project_id"],
                                             run_id=run_id, actor=self._actor(ctx))

    def answer(self, caller: Caller, run_id: str, answers: list, *, surface: str) -> None:
        from ..db import begin
        from ..services import a2a_server

        ctx = self._ctx(caller, surface)
        with begin(self.engine) as c:
            a2a_server.answer_in_band(c, self.agent, run_id, answers, auth=ctx)

    def events(self, caller: Caller, run_id: str, *, timeout: float,
               surface: str) -> Iterator[dict]:
        from ..services import agent_protocol

        self._ctx(caller, surface)
        return agent_protocol.stream(self.engine, self.agent["project_id"], run_id,
                                     timeout=timeout)


__all__ = ["A2AError", "Backend", "Caller", "InProcessBackend", "ServingError", "ServingRequest",
           "ServingResponse", "a2a_handle", "a2a_stream", "checkpoint_events", "dispatch",
           "final_events", "interrupt_events", "match", "metadata_event", "sse", "sse_data",
           "stopped", "task_from_view", "waits_on_a_person"]
