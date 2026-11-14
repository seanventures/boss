"""ACT-PARENTED SPANS FOR EVERY RUN (AP-P4, BOSSAPP-1055; docs/design/AGENT_PLATFORM.md §4.8).

The trace tree already existed in data — agent_acts parent links joined to llm_calls and
tool_call_audit through act_id — but nothing could draw it: one act per run, no step, no timing
of a tool, nothing for an interrupt. This module is the `trace` governance middleware: appended
to every stack by `governance_middleware` (mandatory, like the receipt), it writes one span per

    run        before_agent .. after_agent           the root; `traceparent` names it
    step       each before_model .. the next one      a model call and the tool calls it asked
    model      each model.response / model.error      names its llm_calls row by (run, seq)
    tool       wrap_tool_call                         subagent / handoff by the tool's kind
    interrupt  a held call (a Decision) or a budget   ends when the run resumes
               park, until the run resumes

COST IS NEVER COPIED. A model span carries the ledger `seq` of its event; the llm_calls row the
ledger sink writes for that event has the same (run_id, seq). The trace reader joins them, so the
spans of a run sum to the run's llm_calls total by construction (services/traces.py).

IDS ARE W3C TRACE CONTEXT. trace 32 hex, span 16 hex; `traceparent(...)` is the header value.
A run started with `extra["traceparent"]` (a parent run's tool span, an MCP caller's header)
joins that trace as its child; a run started INSIDE another run's tool call (a subagent run in
process) finds the caller's span in `CURRENT` and joins it the same way, with no plumbing. While a
tool call runs, `CURRENT` holds its traceparent, so an MCP call made inside it forwards a
traceparent that names the run (mcp_headers_policy.current_traceparent), OTel export or not.

WHERE SPANS GO. A sink with `write_span` (MemorySpanSink in tests) gets them; a ledger DbSink
(the run has a database) hands them to services/traces.DbSpanSink, which redacts and stores the
payloads and exports the span over OTLP when BOSS's own tracing is on (off by default, D8). With
neither, spans are not kept. A span write that fails is logged; it never fails the run.

Persistent state lives under run.state.extra["_trace"] (the checkpoint carries it: a resumed run
continues its spans); the model call's input waits on the middleware instance between the wrap
and after_model of ONE call (transient: lost only if the process dies mid-call).
"""
from __future__ import annotations

import contextvars
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("boss.kernel.trace")

KINDS = ("run", "step", "model", "tool", "subagent", "interrupt", "handoff")
STATUSES = ("running", "ok", "error", "refused", "held", "cancelled", "skipped")

#: Tools that start another agent's run (subagents-as-tools, design §4.5) or hand the
#: conversation over. A tool may also say so itself: ToolSchema.kind "subagent" / "handoff".
SUBAGENT_TOOLS = frozenset({"task", "run_subagent", "delegate", "delegate_to_agent", "ask_agent",
                            "consult_agent"})
HANDOFF_TOOLS = frozenset({"handoff", "hand_off", "transfer_to_agent"})

#: The traceparent of the span code is running inside (a tool call of a kernel run), or None.
CURRENT: contextvars.ContextVar = contextvars.ContextVar("boss_trace_current", default=None)

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_KEY = "_trace"
#: The largest new-message delta a model span's input keeps (each message whole, redacted later).
MAX_INPUT_MESSAGES = 40


# ── W3C trace context ──────────────────────────────────────────────────────────────────────────

def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def traceparent(trace_id: str, span_id: str, *, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(value: Any) -> Optional[tuple[str, str]]:
    """(trace_id, parent_span_id) from a W3C traceparent, or None when it is not one (all-zero
    ids are invalid by the spec)."""
    found = _TRACEPARENT.match(str(value or "").strip().lower())
    if not found or set(found.group(1)) == {"0"} or set(found.group(2)) == {"0"}:
        return None
    return found.group(1), found.group(2)


# ── The span ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    project_id: str
    run_id: str
    kind: str
    name: str
    started_at: float
    status: str = "running"
    step: int = 0
    act_id: Optional[str] = None
    seq: Optional[int] = None
    child_run_id: Optional[str] = None
    decision_id: Optional[str] = None
    agent_key: Optional[str] = None
    ended_at: Optional[float] = None
    attributes: dict = field(default_factory=dict)
    #: Full payloads, unredacted here: the sink redacts before anything is stored.
    input: Any = None
    output: Any = None

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        return max(0, int((self.ended_at - self.started_at) * 1000))

    @property
    def traceparent(self) -> str:
        return traceparent(self.trace_id, self.span_id)

    def as_dict(self) -> dict:
        out = asdict(self)
        out["duration_ms"] = self.duration_ms
        out["started_at"] = _iso(self.started_at)
        out["ended_at"] = _iso(self.ended_at) if self.ended_at is not None else None
        return out


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class MemorySpanSink:
    """Spans kept in a dict by id, the last write winning (tests, runs with no database). Also a
    ledger sink (`write`) so one object can be a RunSpec's `sink`."""

    def __init__(self) -> None:
        self.spans: dict = {}
        self.events: list = []

    def write_span(self, span: Span) -> None:
        """An upsert, as the database's: attributes merge, a payload is kept unless replaced."""
        copy = Span(**asdict(span))
        prior = self.spans.get(span.span_id)
        if prior is not None:
            copy.attributes = {**prior.attributes, **copy.attributes}
            copy.input = copy.input if copy.input is not None else prior.input
            copy.output = copy.output if copy.output is not None else prior.output
        self.spans[span.span_id] = copy

    def next_seq(self) -> Optional[int]:
        return None

    def write(self, event: Any, state: Any, meta: Any) -> None:
        self.events.append(event)

    def tree(self) -> list:
        """The spans as (depth, span) in tree order, for tests and debugging."""
        children: dict = {}
        for span in self.spans.values():
            children.setdefault(span.parent_span_id, []).append(span)
        for kids in children.values():
            kids.sort(key=lambda s: (s.started_at, s.span_id))
        ids = set(self.spans)
        roots = [s for s in self.spans.values() if s.parent_span_id not in ids]
        out: list = []

        def walk(span: Span, depth: int) -> None:
            out.append((depth, span))
            for kid in children.get(span.span_id, []):
                walk(kid, depth + 1)

        for root in sorted(roots, key=lambda s: s.started_at):
            walk(root, 0)
        return out


def span_sink_for(sink: Any) -> Any:
    """Where a run's spans go: the run's sink when it takes spans, the database behind a ledger
    DbSink, else nowhere (None)."""
    if sink is None:
        return None
    if callable(getattr(sink, "write_span", None)):
        return sink
    engine = getattr(sink, "engine", None)
    if engine is not None:
        try:
            from ..services.traces import DbSpanSink

            return DbSpanSink(engine)
        except Exception as exc:  # noqa: BLE001 — a run without its spans is still a run
            log.warning("trace: no span sink (non-fatal): %s", exc)
    return None


# ── The middleware ─────────────────────────────────────────────────────────────────────────────

def _agent_key(spec: Any) -> Optional[str]:
    extra = getattr(spec, "extra", None) or {}
    agent_spec = extra.get("agent_spec") if isinstance(extra, dict) else None
    if isinstance(agent_spec, dict) and agent_spec.get("key"):
        return str(agent_spec["key"])
    step = extra.get("agent_step") if isinstance(extra, dict) else None
    if isinstance(step, dict) and step.get("role_key"):
        return str(step["role_key"])
    actor = str(getattr(spec, "actor", "") or "")
    return actor.split(":", 1)[1] if actor.startswith("agent:") else None


def _tool_kind(name: str, schema: Any) -> str:
    declared = str(getattr(schema, "kind", "") or "")
    if declared in ("subagent", "handoff"):
        return declared
    if name in SUBAGENT_TOOLS:
        return "subagent"
    if name in HANDOFF_TOOLS or name.startswith("transfer_to_"):
        return "handoff"
    return "tool"


def _child_run(content: Any) -> Optional[str]:
    if isinstance(content, dict):
        for key in ("child_run_id", "run_id", "agent_run_id"):
            if content.get(key):
                return str(content[key])
    return None


class Trace:
    """See the module docstring. Mandatory: governance appends it, outermost of the governance
    wraps (so a refused, held or screened call is a span), and its after_model runs AFTER the
    telemetry receipt's (node hooks run in reverse after), so the model.response it links to is
    already numbered."""

    name = "trace"
    version = "trace@1"
    can_jump_to: tuple = ()

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._pending_input: Optional[dict] = None
        self._sink: Any = None
        self._sink_for: Any = object()

    # ── plumbing ──────────────────────────────────────────────────────────────────────────
    def sink(self, run: Any) -> Any:
        if self._sink_for is not run:
            self._sink_for = run
            self._sink = span_sink_for(getattr(run.spec, "sink", None))
        return self._sink

    @staticmethod
    def ctx(run: Any) -> dict:
        extra = run.state.extra
        if not isinstance(extra.get(_KEY), dict):
            extra[_KEY] = {}
        return extra[_KEY]

    def _write(self, run: Any, span: Span) -> None:
        sink = self.sink(run)
        if sink is None:
            return
        try:
            sink.write_span(span)
        except Exception as exc:  # noqa: BLE001 — the trace never stops the work
            log.warning("trace: span %s not written (non-fatal): %s", span.kind, exc)

    def _span(self, run: Any, kind: str, name: str, *, parent: Optional[str],
              started: Optional[float] = None, **fields: Any) -> Span:
        ctx = self.ctx(run)
        state = run.state
        return Span(span_id=new_span_id(), trace_id=ctx["trace_id"], parent_span_id=parent,
                    project_id=state.project_id, run_id=state.run_id, kind=kind, name=name,
                    started_at=self.clock() if started is None else started, step=state.step,
                    act_id=state.act_id, agent_key=ctx.get("agent_key"), **fields)

    def _parent(self, run: Any) -> Optional[str]:
        ctx = self.ctx(run)
        return (ctx.get("step") or {}).get("id") or ctx.get("run_span")

    # ── before_agent: the run span (or its resumption) ───────────────────────────────────
    def before_agent(self, run: Any) -> None:
        ctx = self.ctx(run)
        spec, state = run.spec, run.state
        if ctx.get("run_span"):
            self._resumed(run)
            return None
        extra = getattr(spec, "extra", None) or {}
        parent = parse_traceparent(extra.get("traceparent") if isinstance(extra, dict) else None)
        if parent is None:
            parent = parse_traceparent(CURRENT.get())
        ctx["trace_id"], parent_id = parent if parent else (new_trace_id(), None)
        ctx["agent_key"] = _agent_key(spec)
        ctx["msg_index"] = 0
        ctx["last_seq"] = 0
        span = self._span(run, "run", str(spec.stage or spec.actor or "run"), parent=parent_id,
                          attributes={"actor": spec.actor, "stage": spec.stage,
                                      "scaffold_level": spec.scaffold_level,
                                      "lineage_id": state.lineage_id, "attempt": state.attempt,
                                      "engine": str(getattr(spec.engine, "name", "") or ""),
                                      "middleware": run.chain.names(),
                                      "agent_spec": extra.get("agent_spec")
                                      if isinstance(extra, dict) else None},
                          input={"system": [getattr(b, "text", str(b)) for b in state.system],
                                 "messages": list(state.messages)})
        ctx["run_span"] = span.span_id
        ctx["run_started"] = span.started_at
        ctx["run_parent"] = parent_id
        self._write(run, span)
        return None

    def _resumed(self, run: Any) -> None:
        """A resume: the interrupt the run waited on ends (the person answered, the budget
        freed), and the run span is running again."""
        ctx = self.ctx(run)
        held = ctx.pop("interrupt", None)
        # AP-P2: every interrupt the run waited on (parallel holds, an unknown outcome) ends.
        others = [i for i in ctx.pop("interrupts", None) or []
                  if isinstance(i, dict) and i.get("id") != (held or {}).get("id")]
        now = self.clock()
        for saved in ([held] if isinstance(held, dict) else []) + others:
            span = self._restore(run, saved, kind="interrupt")
            span.status, span.ended_at = "ok", now
            span.attributes = {**span.attributes, "resumed": True}
            self._write(run, span)
        ctx.pop("step", None)
        run_span = self._run_span(run)
        run_span.status = "running"
        # AP-P2: which checkpoint the run went on from, and a crashed batch it finished first.
        run_span.attributes = {**run_span.attributes, "resumed_at": _iso(now),
                               "resumed_from_checkpoint": getattr(run.state, "checkpoint_id",
                                                                  None),
                               **({"resumed_batch": run.resumed_batch}
                                  if getattr(run, "resumed_batch", None) else {})}
        self._write(run, run_span)

    def _restore(self, run: Any, saved: dict, *, kind: str) -> Span:
        ctx = self.ctx(run)
        return Span(span_id=saved["id"], trace_id=ctx["trace_id"],
                    parent_span_id=saved.get("parent"), project_id=run.state.project_id,
                    run_id=run.state.run_id, kind=kind, name=str(saved.get("name") or kind),
                    started_at=float(saved.get("started") or self.clock()),
                    step=int(saved.get("step") or 0), act_id=run.state.act_id,
                    agent_key=ctx.get("agent_key"), decision_id=saved.get("decision_id"),
                    attributes=dict(saved.get("attributes") or {}))

    def _run_span(self, run: Any) -> Span:
        ctx = self.ctx(run)
        spec = run.spec
        return Span(span_id=ctx["run_span"], trace_id=ctx["trace_id"],
                    parent_span_id=ctx.get("run_parent"), project_id=run.state.project_id,
                    run_id=run.state.run_id, kind="run",
                    name=str(spec.stage or spec.actor or "run"),
                    started_at=float(ctx.get("run_started") or self.clock()),
                    act_id=run.state.act_id, agent_key=ctx.get("agent_key"),
                    step=run.state.step)

    # ── steps ─────────────────────────────────────────────────────────────────────────────
    def _close_step(self, run: Any, status: str = "ok") -> None:
        ctx = self.ctx(run)
        step = ctx.pop("step", None)
        if not isinstance(step, dict):
            return
        span = self._restore(run, step, kind="step")
        span.status, span.ended_at = status, self.clock()
        self._write(run, span)

    def before_model(self, run: Any, call: Any) -> None:
        self._drain(run)
        self._close_step(run)
        ctx = self.ctx(run)
        number = int(run.state.step) + 1
        span = self._span(run, "step", f"step {number}", parent=ctx.get("run_span"),
                          attributes={"answering": bool(getattr(call, "answering", False))})
        span.step = number
        ctx["step"] = {"id": span.span_id, "parent": span.parent_span_id, "name": span.name,
                       "started": span.started_at, "step": number,
                       "attributes": span.attributes}
        self._write(run, span)
        return None

    # ── model calls ───────────────────────────────────────────────────────────────────────
    def wrap_model_call(self, run: Any, call: Any, handler: Callable) -> Any:
        self._drain(run)
        ctx = self.ctx(run)
        request = getattr(call, "request", None)
        messages = list(getattr(request, "messages", None) or run.state.messages)
        start = int(ctx.get("msg_index") or 0)
        delta = messages[start:] if start <= len(messages) else messages
        self._pending_input = {
            "started": self.clock(),
            "payload": {"messages_total": len(messages),
                        "new_messages": delta[-MAX_INPUT_MESSAGES:],
                        "tools": [t.name for t in (getattr(request, "tools", None) or [])],
                        "answering": bool(getattr(call, "answering", False)),
                        "attempt": int(getattr(call, "notes", {}).get("attempt") or 1)}}
        return handler(call)

    def after_model(self, run: Any, turn: Any) -> None:
        output = {"text": getattr(turn, "text", "") or "",
                  "tool_calls": [{"name": c.name, "arguments": c.arguments}
                                 for c in (getattr(turn, "tool_calls", None) or [])],
                  "stop_reason": getattr(turn, "stop_reason", None)}
        self._drain(run, output=output)
        self.ctx(run)["msg_index"] = len(run.state.messages)
        return None

    def _drain(self, run: Any, *, output: Any = None) -> None:
        """Every model.response / model.error the run recorded since the last span, as a model
        span (the response gets `output`, the turn it came back with)."""
        ctx = self.ctx(run)
        last = int(ctx.get("last_seq") or 0)
        for event in list(getattr(run.rec, "events", None) or []):
            if event.seq <= last or event.kind not in ("model.response", "model.error"):
                continue
            ctx["last_seq"] = last = int(event.seq)
            pending, self._pending_input = self._pending_input, None
            at = getattr(event, "at", None)
            ended = at.timestamp() if isinstance(at, datetime) else self.clock()
            started = (pending or {}).get("started") or ended - (event.latency_ms or 0) / 1000
            usage = event.usage
            model = str(getattr(usage, "model", "") or "") or str(
                getattr(run.spec.engine, "model", "") or "")
            ok = event.kind == "model.response"
            span = self._span(run, "model", f"model {model or 'call'}", parent=self._parent(run),
                              started=started, seq=int(event.seq),
                              status="ok" if ok else "error",
                              attributes={"model": model or None,
                                          "latency_ms": int(event.latency_ms or 0),
                                          **({"usage": asdict(usage)} if usage is not None
                                             else {}),
                                          **({} if ok else {"error": event.data.get("error"),
                                                            "class": event.data.get("class"),
                                                            "action": event.data.get("action")})},
                              input=(pending or {}).get("payload"),
                              output=output if ok else {"error": event.data.get("error")})
            span.step = int(event.step or run.state.step)
            span.ended_at = ended
            self._write(run, span)

    # ── tool calls ────────────────────────────────────────────────────────────────────────
    def wrap_tool_call(self, run: Any, request: Any, handler: Callable) -> Any:
        call = request.call
        kind = _tool_kind(call.name, request.schema)
        span = self._span(run, kind, call.name, parent=self._parent(run),
                          attributes={"call_id": call.id,
                                      "write": getattr(request.schema, "kind", None) == "write",
                                      "approved": request.approved is not None},
                          input={"arguments": call.arguments})
        span.step = int(request.step or run.state.step)
        token = CURRENT.set(span.traceparent)
        try:
            result = handler(request)
        except Exception as exc:
            span.status, span.ended_at = "error", self.clock()
            span.output = {"error": f"{type(exc).__name__}: {exc}"[:600]}
            self._write(run, span)
            raise
        finally:
            CURRENT.reset(token)
        status = str(getattr(result, "status", "ok") or "ok")
        span.ended_at = self.clock()
        span.status = {"deferred": "held"}.get(status, status if status in STATUSES else "ok")
        span.output = {"status": status, "content": getattr(result, "content", None),
                       "untrusted": bool(getattr(result, "untrusted", False))}
        span.decision_id = getattr(result, "decision_id", None)
        span.child_run_id = _child_run(getattr(result, "content", None)) \
            if kind in ("subagent", "handoff") else None
        self._write(run, span)
        if status == "deferred":
            self._interrupt(run, span, result)
        return result

    def _interrupt(self, run: Any, tool: Span, result: Any) -> None:
        """A held call is an interrupt: a span that stays open until the run resumes."""
        budget = getattr(result, "budget", None)
        pending = getattr(result, "pending", None) or {}
        declared = pending.get("interrupt") if isinstance(pending.get("interrupt"), dict) else {}
        self.interrupt_opened(
            run, "budget" if budget else f"{declared.get('kind') or 'approval'} {tool.name}",
            decision_id=getattr(result, "decision_id", None),
            attributes={"tool": tool.name, "tool_span": tool.span_id, "budget": budget,
                        "on": "budget" if budget else "decision",
                        "interrupt_id": declared.get("id") or pending.get("interrupt_id")})

    def interrupt_opened(self, run: Any, name: str, *, decision_id: Optional[str] = None,
                         attributes: Optional[dict] = None) -> None:
        """ONE SPAN PER INTERRUPT (AP-P2, kernel/interrupts.py): open until the run resumes.
        Parallel holds in one step are each their own span; the first is also `interrupt`."""
        ctx = self.ctx(run)
        span = self._span(run, "interrupt", name, parent=self._parent(run),
                          decision_id=decision_id, attributes=dict(attributes or {}))
        span.status = "held"
        saved = {"id": span.span_id, "parent": span.parent_span_id, "name": span.name,
                 "started": span.started_at, "step": span.step,
                 "decision_id": span.decision_id, "attributes": span.attributes}
        if not isinstance(ctx.get("interrupt"), dict):
            ctx["interrupt"] = saved
        ctx.setdefault("interrupts", []).append(saved)
        self._write(run, span)

    # ── after_agent: the run span closes (or waits) ──────────────────────────────────────
    def after_agent(self, run: Any, result: Any) -> None:
        self._drain(run)
        ctx = self.ctx(run)
        if not ctx.get("run_span"):
            return None
        status = str(getattr(result, "status", "") or "")
        waiting = status in ("suspended", "budget_wait")
        self._close_step(run, "held" if waiting else "ok")
        if status == "budget_wait" and not ctx.get("interrupt"):
            pending = getattr(result, "pending", None) or {}
            span = self._span(run, "interrupt", "budget", parent=ctx.get("run_span"),
                              attributes={"on": "budget", "budget": pending.get("budget")})
            span.status = "held"
            ctx["interrupt"] = {"id": span.span_id, "parent": span.parent_span_id,
                                "name": span.name, "started": span.started_at,
                                "step": span.step, "attributes": span.attributes}
            self._write(run, span)
        span = self._run_span(run)
        span.status = {"completed": "ok", "failed": "error", "cancelled": "cancelled",
                       "suspended": "held", "budget_wait": "held"}.get(status, "ok")
        span.ended_at = self.clock()
        span.attributes = {"status": status, "stopped": getattr(result, "stopped", ""),
                           "steps": getattr(result, "steps", 0),
                           "error": (getattr(result, "error", "") or "")[:600] or None}
        span.output = {"text": getattr(result, "text", ""),
                       "structured": getattr(result, "structured", None),
                       "pending": getattr(result, "pending", None)}
        self._write(run, span)
        return None


__all__ = ["CURRENT", "HANDOFF_TOOLS", "KINDS", "MemorySpanSink", "STATUSES", "SUBAGENT_TOOLS",
           "Span", "Trace", "new_span_id", "new_trace_id", "parse_traceparent", "span_sink_for",
           "traceparent"]
