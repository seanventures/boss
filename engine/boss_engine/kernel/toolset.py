"""The tool seam: what a run may call, and the one place a call is decided and run.

Each old loop gated tool calls its own way — agent_loop's identity refusal, untrusted-content
hold and MCP deferral; EngineAgent's write hold and propose-only offers; the decision loop's
read-only runner; the native coder's sandbox and tool gateway. Those gates stay where they are
and are put BEHIND this one interface (design §2.4), so the loop around them is written once.

A ToolSet returns schemas for the step and invokes one call. What a tool returned is normalised
to a ToolResult here, so "the run waits for a person", "the budget is busy" and "that was
refused" mean the same thing whatever produced them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .state import RunState
from .types import ToolCall, ToolResult, ToolSchema

#: The statuses old tools return that mean "a person must approve this first".
_DEFERRED = ("deferred_for_approval", "awaiting_approval")
#: ... "the budget is busy; the run waits" (agent_loop.BUDGET_WAIT).
_BUDGET_WAIT = "budget_wait"
#: ... "this caller may not do that".
_REFUSED = ("refused", "denied", "forbidden")


class ToolSet(Protocol):
    def schemas(self, *, step: int, state: RunState) -> list: ...

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult: ...


def result_from(call: ToolCall, value: Any) -> ToolResult:
    """A tool's own return value as a ToolResult. A ToolResult passes through untouched."""
    if isinstance(value, ToolResult):
        return value
    if not isinstance(value, Mapping):
        return ToolResult(call.id, call.name, "ok", value)
    status = str(value.get("status") or "")
    if status in _DEFERRED and value.get("decision_id"):
        return ToolResult(call.id, call.name, "deferred", dict(value),
                          untrusted=bool(value.get("untrusted")),
                          decision_id=str(value["decision_id"]),
                          pending=value.get("pending") if isinstance(value.get("pending"),
                                                                     dict) else None)
    if status == _BUDGET_WAIT:
        budget = value.get("budget") if isinstance(value.get("budget"), dict) else dict(value)
        return ToolResult(call.id, call.name, "deferred", dict(value), budget=budget)
    if status in _REFUSED or value.get("error") == "forbidden":
        return ToolResult(call.id, call.name, "refused", dict(value))
    if status == "skipped":
        return ToolResult(call.id, call.name, "skipped", dict(value))
    if value.get("ok") is False or value.get("error"):
        return ToolResult(call.id, call.name, "error", dict(value))
    return ToolResult(call.id, call.name, "ok", dict(value))


def failed(call: ToolCall, exc: BaseException, *, limit: int = 400) -> ToolResult:
    return ToolResult(call.id, call.name, "error",
                      {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:limit]})


@dataclass(frozen=True)
class Tool:
    """One tool of a FunctionToolSet. `run_approved` runs the call a person approved (resume),
    without asking again whatever `run` would have held it for."""

    schema: ToolSchema
    run: Callable[[dict], Any]
    run_approved: Optional[Callable[[dict], Any]] = None


class FunctionToolSet:
    """Tools as plain functions of their arguments: the shape tests, the scripted benchmark and
    small callers need."""

    def __init__(self, tools: Sequence[Tool], *, error_chars: int = 400):
        self._tools = {t.schema.name: t for t in tools}
        self._error_chars = error_chars

    def schemas(self, *, step: int, state: RunState) -> list:
        return [t.schema for t in self._tools.values()]

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return unknown(call)
        fn = tool.run_approved if approved is not None and tool.run_approved else tool.run
        try:
            return result_from(call, fn(dict(call.arguments or {})))
        except Exception as exc:  # noqa: BLE001 — a tool that raises is a result the model reads
            return failed(call, exc, limit=self._error_chars)


class ReadOnlyToolSet:
    """agent_decision.ReadPhase and the pre-check's read tools: OpenAI-shaped schemas and one
    `run(name, args)` callable. Reads only; a failure is `{"ok": false, "error": ...}` as both
    loops reported it."""

    def __init__(self, tools: Sequence[Mapping[str, Any]], run: Callable[[str, dict], Any], *,
                 error_chars: int = 300):
        self._schemas = [ToolSchema.from_openai(t) for t in tools or ()]
        self._run = run
        self._error_chars = error_chars

    def schemas(self, *, step: int, state: RunState) -> list:
        return list(self._schemas)

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        try:
            value = self._run(call.name, dict(call.arguments or {}))
        except Exception as exc:  # noqa: BLE001
            return failed(call, exc, limit=self._error_chars)
        return result_from(call, value)


class RegistryToolSet:
    """BOSS's tool registry (tools/registry.py) as a ToolSet. `invoke` goes through
    `ToolRegistry.invoke`, which runs identity_gate (person ∩ grant ∩ run token).

    `run_tool(name, args, state)` is the caller's own gate when it has one: EngineAgent passes
    its `_run_tool` (write hold, propose-only offers, the untrusted-content release, consent
    offers) when chat moves onto the kernel (K6), so that code moves whole instead of being
    copied here. Without it, a write is held as a pending offer unless `allow_writes`, which is
    EngineAgent's rule in its simplest form."""

    def __init__(self, registry: Any, ctx: Any, *, project_id: str, actor: str,
                 allow_writes: bool = False, exclude: Sequence[str] = (),
                 run_tool: Optional[Callable[[str, dict, RunState], Any]] = None):
        self.registry = registry
        self.ctx = ctx
        self.project_id = project_id
        self.actor = actor
        self.allow_writes = allow_writes
        self.exclude = frozenset(exclude)
        self.run_tool = run_tool
        self.pending: list = []

    def schemas(self, *, step: int, state: RunState) -> list:
        return [ToolSchema(name=s.name, description=s.description, parameters=dict(s.parameters),
                           kind=s.kind, reversibility=getattr(s, "reversibility", "safe"))
                for s in self.registry.specs() if s.name not in self.exclude]

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        args = dict(call.arguments or {})
        if call.name in self.exclude:
            return unknown(call)
        try:
            if self.run_tool is not None:
                return result_from(call, self.run_tool(call.name, args, state))
            try:
                spec = self.registry.get(call.name)
            except Exception as exc:  # noqa: BLE001 — registry.ToolError: no such tool
                if "unknown tool" in str(exc):
                    return unknown(call)
                raise
            if spec.kind == "write" and not self.allow_writes and approved is None:
                self.pending.append({"tool": call.name, "args": args})
                return ToolResult(call.id, call.name, "ok", {
                    "ok": False, "status": "confirmation_required",
                    "note": f"'{call.name}' NOT run; a write never finds anything out.",
                    "proposed": {"tool": call.name, "args": args}})
            return result_from(call, self.registry.invoke(
                call.name, self.ctx, project_id=self.project_id, actor=self.actor, args=args,
                operator_confirmed=approved is not None))
        except Exception as exc:  # noqa: BLE001
            return failed(call, exc)


class Composite:
    """Several tool sets as one: schemas in order, each call routed to the set that offered its
    name first. The kernel's built-ins (read_run_ledger) join this way."""

    def __init__(self, *sets: Any):
        self.sets = [s for s in sets if s is not None]

    def schemas(self, *, step: int, state: RunState) -> list:
        seen: set = set()
        out: list = []
        for toolset in self.sets:
            for schema in toolset.schemas(step=step, state=state):
                if schema.name not in seen:
                    seen.add(schema.name)
                    out.append(schema)
        return out

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        for toolset in self.sets:
            if any(s.name == call.name for s in toolset.schemas(step=state.step, state=state)):
                return toolset.invoke(call, state=state, approved=approved)
        return unknown(call)


def unknown(call: ToolCall) -> ToolResult:
    return ToolResult(call.id, call.name, "refused", {
        "ok": False, "status": "refused", "error": "unknown_tool",
        "message": f"'{call.name}' is not one of the tools offered to you. Use one that is."})


__all__ = ["Composite", "FunctionToolSet", "ReadOnlyToolSet", "RegistryToolSet", "Tool",
           "ToolSet", "failed", "result_from", "unknown"]
