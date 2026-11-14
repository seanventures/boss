"""REMOTE AGENTS AS BOSS SUB-AGENTS: A2A agents and Agent Protocol servers (AP-P5, BOSSAPP-1056;
AGENT_PLATFORM §4.5 "Outbound": "A2A remote agents and Agent Protocol servers (including
LangGraph deployments) become ... a subagent target").

THE ADAPTER AP-P7's `task` tool calls when its target is remote. One shape in and out, whatever
the protocol:

    ref    RemoteAgentRef(protocol "a2a" | "agent_protocol", url, name, agent_id?, flavor?,
           auth_header?)  — `auth_header` is resolved by the CALLER from a secret reference;
           it is sent as `Authorization` and never logged, stored or returned.
    out    an A2A Task (protocols/a2a_types.Task.to_dict()) — the same shape P7's local async
           sub-agents return — plus the capped report, the cost and the span.

    run(...)      a synchronous sub-agent: send the brief, wait until the remote task ends or
                  stops for input (or `timeout`), return {task, report, cost_usd_micros,
                  span_id, handle, timed_out}.
    start(...)    an async sub-agent: send and return at once ({task, span_id, handle}).
    check(...)    the task now (and, when it has ended, its report and cost, recorded once).
    update(...)   answer a remote task that asked for input (a follow-up message on it).
    cancel(...)   cancel it.

EVERY CALL IS RECORDED AS A REMOTE SPAN ON THE PARENT RUN (AP-P4 traces):

  * a `trace_spans` row, kind `subagent`, named `remote:<name>`, under the parent run's trace
    (the span in kernel.trace.CURRENT when called from inside a tool call, else the parent run's
    root span, else a new trace), attributes {remote, protocol, endpoint_host, remote_task_id,
    remote_context_id, state}; status ok / error / held (input required) / cancelled. Its
    traceparent is forwarded to the remote, so the remote's own spans join BOSS's trace;
  * the remote's reported cost as an `llm_calls` row of the parent run (stage `remote_agent`,
    engine `a2a` | `agent_protocol`, model = the remote agent's name), once per remote task, so
    the parent run's trace still reconciles with its ledger (services/traces.run_trace shows the
    row as a ledger model span).

The remote reports cost in its task/run metadata: `cost_usd_micros`, `usage.cost_usd_micros`,
or `cost_usd` (dollars); tokens from `usage` when present. A remote that reports nothing is
recorded at zero (its spend is not BOSS's to know).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from sqlalchemy import text

from ..kernel import trace as ktrace
from ..protocols import a2a_types as t

log = logging.getLogger(__name__)

PROTOCOLS = ("a2a", "agent_protocol")
#: The report a sub-agent hands back to its parent (≈1,500 tokens, design §4.5).
REPORT_CHARS = 6000
POLL_SECONDS = 0.5


class RemoteAgentError(RuntimeError):
    pass


@dataclass
class RemoteAgentRef:
    protocol: str
    url: str
    name: str
    agent_id: Optional[str] = None
    flavor: str = "agent_protocol"      # agent_protocol | langgraph (protocol agent_protocol)
    auth_header: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise RemoteAgentError(f"protocol must be one of {PROTOCOLS}")
        if not self.url or not self.name:
            raise RemoteAgentError("a remote agent needs a url and a name")

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc

    def headers(self) -> dict:
        return {"Authorization": self.auth_header} if self.auth_header else {}


# ── clients ────────────────────────────────────────────────────────────────────────────────────

def _a2a(ref: RemoteAgentRef, transport: Any):
    from ..protocols.a2a_client import A2AClient

    return A2AClient(ref.url, headers=ref.headers(), transport=transport)


def _ap(ref: RemoteAgentRef, transport: Any):
    from ..protocols.agent_protocol_client import AgentProtocolClient

    return AgentProtocolClient(ref.url, agent_id=ref.agent_id, flavor=ref.flavor,
                               headers=ref.headers(), transport=transport)


def _as_task(obj: Any) -> t.Task:
    """SendMessage may answer with a direct Message: that is a finished task."""
    if isinstance(obj, t.Task):
        return obj
    if isinstance(obj, t.Message):
        return t.Task(id=obj.task_id or f"msg:{obj.message_id}", context_id=obj.context_id,
                      status=t.TaskStatus(t.TaskState.COMPLETED, obj), history=[obj])
    raise RemoteAgentError("the remote agent answered neither a task nor a message")


# ── what is recorded ───────────────────────────────────────────────────────────────────────────

def report_of(task: t.Task) -> str:
    """The text the parent reads: the artifacts, else the status message, else the last agent
    message; capped."""
    texts = [p.text for a in task.artifacts for p in a.parts if p.text]
    if not texts and task.status.message is not None:
        texts = [task.status.message.text()]
    if not texts:
        agent = [m for m in task.history if m.role is t.Role.AGENT]
        texts = [agent[-1].text()] if agent else []
    out = "\n".join(x for x in texts if x).strip()
    return out if len(out) <= REPORT_CHARS else out[:REPORT_CHARS - 1] + "…"


def cost_of(metadata: Optional[Mapping[str, Any]]) -> dict:
    """{cost_usd_micros, prompt_tokens, completion_tokens} from what the remote reported."""
    meta = metadata if isinstance(metadata, Mapping) else {}
    usage = meta.get("usage") if isinstance(meta.get("usage"), Mapping) else {}
    micros = meta.get("cost_usd_micros", usage.get("cost_usd_micros"))
    if micros is None:
        dollars = meta.get("cost_usd", usage.get("cost_usd"))
        micros = round(float(dollars) * 1_000_000) if dollars is not None else 0
    try:
        micros = max(0, int(micros))
    except (TypeError, ValueError):
        micros = 0

    def tokens(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
        return 0

    return {"cost_usd_micros": micros,
            "prompt_tokens": tokens("input_tokens", "prompt_tokens"),
            "completion_tokens": tokens("output_tokens", "completion_tokens")}


_SPAN_STATUS = {t.TaskState.COMPLETED: "ok", t.TaskState.FAILED: "error",
                t.TaskState.REJECTED: "refused", t.TaskState.CANCELED: "cancelled",
                t.TaskState.INPUT_REQUIRED: "held", t.TaskState.AUTH_REQUIRED: "held"}


def _parent(engine: Any, project_id: str, parent_run_id: str) -> tuple:
    """(trace_id, parent_span_id) the remote span joins."""
    current = ktrace.parse_traceparent(ktrace.CURRENT.get())
    if current is not None:
        return current
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT trace_id, span_id FROM trace_spans WHERE project_id = :p AND run_id = :r "
            "AND kind = 'run' ORDER BY started_at LIMIT 1"),
            {"p": project_id, "r": parent_run_id}).first()
    if row is not None:
        return str(row[0]), str(row[1])
    return ktrace.new_trace_id(), None


def _span(engine: Any, *, project_id: str, parent_run_id: str, ref: RemoteAgentRef,
          brief: str, traceparent: Optional[str]) -> ktrace.Span:
    parsed = ktrace.parse_traceparent(traceparent) if traceparent else None
    trace_id, parent = parsed if parsed else _parent(engine, project_id, parent_run_id)
    return ktrace.Span(
        span_id=ktrace.new_span_id(), trace_id=trace_id, parent_span_id=parent,
        project_id=project_id, run_id=parent_run_id, kind="subagent",
        name=f"remote:{ref.name}"[:300], started_at=time.time(),
        attributes={"remote": True, "protocol": ref.protocol, "endpoint_host": ref.host,
                    "remote_agent": ref.name},
        input={"brief": brief})


def _write(engine: Any, span: ktrace.Span) -> None:
    from .traces import DbSpanSink

    try:
        DbSpanSink(engine).write_span(span)
    except Exception:  # noqa: BLE001 — a trace write never fails the sub-agent call
        log.warning("remote sub-agent span %s not written", span.span_id, exc_info=True)


def _load_span(engine: Any, project_id: str, span_id: str) -> Optional[ktrace.Span]:
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT span_id, trace_id, parent_span_id, run_id, name, attributes, started_at "
            "FROM trace_spans WHERE span_id = :s AND project_id = :p"),
            {"s": span_id, "p": project_id}).mappings().first()
    if row is None:
        return None
    attrs = row["attributes"] if isinstance(row["attributes"], dict) else json.loads(
        row["attributes"] or "{}")
    return ktrace.Span(span_id=row["span_id"], trace_id=row["trace_id"],
                       parent_span_id=row["parent_span_id"], project_id=project_id,
                       run_id=row["run_id"], kind="subagent", name=row["name"],
                       started_at=row["started_at"].timestamp(), attributes=dict(attrs))


def _observe(engine: Any, span: ktrace.Span, task: t.Task, ref: RemoteAgentRef) -> None:
    """The span says what the remote task is now; a terminal task ends it and books the cost."""
    state = task.state
    span.attributes.update({"remote_task_id": task.id, "remote_context_id": task.context_id,
                            "state": state.value})
    span.status = _SPAN_STATUS.get(state, "running")
    if state.terminal or state.interrupted:
        span.ended_at = time.time()
        span.output = {"report": report_of(task), "state": state.value}
    _write(engine, span)
    if state.terminal:
        _book_cost(engine, span, task, ref)


def _book_cost(engine: Any, span: ktrace.Span, task: t.Task, ref: RemoteAgentRef) -> dict:
    cost = cost_of(task.metadata)
    latency = int(max(0.0, (span.ended_at or time.time()) - span.started_at) * 1000)
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO llm_calls (id, project_id, run_id, stage, engine, model, prompt_tokens,
                completion_tokens, total_tokens, cost_usd_micros, latency_ms, ok, error)
            VALUES (:id, :p, :r, 'remote_agent', :engine, :model, :pt, :ct, :tt, :cost, :ms,
                :ok, :err)
            ON CONFLICT (id) DO NOTHING"""),
            {"id": f"llm_remote_{span.span_id}", "p": span.project_id, "r": span.run_id,
             "engine": ref.protocol, "model": f"remote:{ref.name}"[:200],
             "pt": cost["prompt_tokens"], "ct": cost["completion_tokens"],
             "tt": cost["prompt_tokens"] + cost["completion_tokens"],
             "cost": cost["cost_usd_micros"], "ms": latency,
             "ok": task.state is t.TaskState.COMPLETED,
             "err": None if task.state is t.TaskState.COMPLETED else task.state.value})
    return cost


def _handle(span: ktrace.Span, task: t.Task, ref: RemoteAgentRef) -> dict:
    return {"protocol": ref.protocol, "task_id": task.id, "context_id": task.context_id,
            "span_id": span.span_id}


# ── the protocol halves ────────────────────────────────────────────────────────────────────────

def _send(ref: RemoteAgentRef, brief: str, traceparent: str, transport: Any, *,
          task_id: Optional[str] = None, context_id: Optional[str] = None) -> t.Task:
    if ref.protocol == "a2a":
        message = t.user_message(brief, task_id=task_id, context_id=context_id)
        request = t.SendMessageRequest(message=message,
                                       configuration=t.SendMessageConfiguration(
                                           return_immediately=True))
        return _as_task(_a2a(ref, transport).send_message(request, traceparent=traceparent))
    client = _ap(ref, transport)
    thread = context_id or client.create_thread(traceparent=traceparent)
    run = client.start_run(thread, {"messages": [{"role": "user", "content": brief}]},
                           traceparent=traceparent)
    from ..protocols.agent_protocol_client import task_of

    return task_of(run)


def _get(ref: RemoteAgentRef, handle: Mapping[str, Any], traceparent: str,
         transport: Any) -> t.Task:
    if ref.protocol == "a2a":
        return _a2a(ref, transport).get_task(str(handle["task_id"]), traceparent=traceparent)
    return _ap(ref, transport).task(str(handle["context_id"]), str(handle["task_id"]),
                                    traceparent=traceparent)


def _cancel(ref: RemoteAgentRef, handle: Mapping[str, Any], traceparent: str,
            transport: Any) -> t.Task:
    if ref.protocol == "a2a":
        return _a2a(ref, transport).cancel_task(str(handle["task_id"]), traceparent=traceparent)
    client = _ap(ref, transport)
    client.cancel_run(str(handle["context_id"]), str(handle["task_id"]),
                      traceparent=traceparent)
    return client.task(str(handle["context_id"]), str(handle["task_id"]),
                       traceparent=traceparent)


# ── the adapter ────────────────────────────────────────────────────────────────────────────────

def start(engine: Any, *, project_id: str, parent_run_id: str, ref: RemoteAgentRef, brief: str,
          traceparent: Optional[str] = None, transport: Any = None) -> dict:
    """An async remote sub-agent: sent, not waited on. {task, span_id, handle}."""
    span = _span(engine, project_id=project_id, parent_run_id=parent_run_id, ref=ref,
                 brief=brief, traceparent=traceparent)
    _write(engine, span)
    try:
        task = _send(ref, brief, span.traceparent, transport)
    except Exception as exc:
        span.status, span.ended_at = "error", time.time()
        span.attributes["error"] = f"{type(exc).__name__}: {exc}"[:300]
        _write(engine, span)
        raise RemoteAgentError(f"{ref.name}: {exc}") from exc
    _observe(engine, span, task, ref)
    return {"task": task.to_dict(), "span_id": span.span_id, "handle": _handle(span, task, ref)}


def check(engine: Any, *, project_id: str, ref: RemoteAgentRef, handle: Mapping[str, Any],
          transport: Any = None) -> dict:
    """The remote task now; once it has ended, its report and the cost (booked once)."""
    span = _load_span(engine, project_id, str(handle["span_id"]))
    if span is None:
        raise RemoteAgentError("no such remote sub-agent span in this project")
    task = _get(ref, handle, span.traceparent, transport)
    _observe(engine, span, task, ref)
    out = {"task": task.to_dict(), "span_id": span.span_id, "handle": dict(handle)}
    if task.state.terminal or task.state.interrupted:
        out["report"] = report_of(task)
        out["cost_usd_micros"] = cost_of(task.metadata)["cost_usd_micros"]
    return out


def update(engine: Any, *, project_id: str, ref: RemoteAgentRef, handle: Mapping[str, Any],
           answer: str, transport: Any = None) -> dict:
    """Answer a remote task waiting for input: a follow-up message on the same task (A2A) or a
    new run on the same thread (Agent Protocol). The span stays the same one."""
    span = _load_span(engine, project_id, str(handle["span_id"]))
    if span is None:
        raise RemoteAgentError("no such remote sub-agent span in this project")
    task = _send(ref, answer, span.traceparent, transport,
                 task_id=str(handle["task_id"]) if ref.protocol == "a2a" else None,
                 context_id=handle.get("context_id"))
    span.ended_at = None
    _observe(engine, span, task, ref)
    return {"task": task.to_dict(), "span_id": span.span_id,
            "handle": {**dict(handle), "task_id": task.id, "context_id": task.context_id}}


def cancel(engine: Any, *, project_id: str, ref: RemoteAgentRef, handle: Mapping[str, Any],
           transport: Any = None) -> dict:
    span = _load_span(engine, project_id, str(handle["span_id"]))
    if span is None:
        raise RemoteAgentError("no such remote sub-agent span in this project")
    task = _cancel(ref, handle, span.traceparent, transport)
    _observe(engine, span, task, ref)
    return {"task": task.to_dict(), "span_id": span.span_id, "handle": dict(handle)}


def run(engine: Any, *, project_id: str, parent_run_id: str, ref: RemoteAgentRef, brief: str,
        timeout: float = 120.0, traceparent: Optional[str] = None, transport: Any = None,
        sleep: Callable[[float], None] = time.sleep) -> dict:
    """A synchronous remote sub-agent: until its task ends or asks for input, or `timeout`."""
    started = start(engine, project_id=project_id, parent_run_id=parent_run_id, ref=ref,
                    brief=brief, traceparent=traceparent, transport=transport)
    deadline = time.monotonic() + max(0.0, float(timeout))
    out = started
    task = t.Task.from_dict(started["task"])
    while not (task.state.terminal or task.state.interrupted):
        if time.monotonic() >= deadline:
            return {**out, "report": report_of(task), "cost_usd_micros": 0, "timed_out": True}
        sleep(POLL_SECONDS)
        out = check(engine, project_id=project_id, ref=ref, handle=started["handle"],
                    transport=transport)
        task = t.Task.from_dict(out["task"])
    return {**out, "report": report_of(task),
            "cost_usd_micros": cost_of(task.metadata)["cost_usd_micros"]
            if task.state.terminal else 0, "timed_out": False}


__all__ = ["PROTOCOLS", "REPORT_CHARS", "RemoteAgentError", "RemoteAgentRef", "cancel", "check",
           "cost_of", "report_of", "run", "start", "update"]
