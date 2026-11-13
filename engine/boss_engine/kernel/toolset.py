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
                 run_tool: Optional[Callable[[str, dict, RunState], Any]] = None,
                 surface: str = "chat", role_key: Optional[str] = None,
                 workflow_id: Optional[str] = None):
        self.registry = registry
        self.ctx = ctx
        self.project_id = project_id
        self.actor = actor
        #: Who is calling, for agent- and workflow-scoped tool-policy rules (BOSSAPP-815). An
        #: actor written `agent:<role>` names its role when none is given.
        self.role_key = role_key or (actor.split(":", 1)[1] if str(actor or "").startswith(
            "agent:") else None)
        self.workflow_id = workflow_id
        self.allow_writes = allow_writes
        self.exclude = frozenset(exclude)
        self.run_tool = run_tool
        self.surface = surface
        self.pending: list = []

    def schemas(self, *, step: int, state: RunState) -> list:
        """Every registry tool as a ToolSchema. With BOSS_SCOPED_TOOLS on (K5,
        kernel/scoped_tools.py), each ToolSpec's `Exposure` decides this surface's view: a tool
        not exposed here is left out, one not CORE here is `deferred` (offered only once
        find_tools returns it). A spec with no exposure, or the flag off, is offered plainly —
        conservative: nothing a surface has today is lost."""
        from .scoped_tools import enabled, surface_view

        if not callable(getattr(self.registry, "specs", None)):
            # A registry that only speaks the adapter shape (chat's test doubles hand
            # EngineAgent an object with `openai_schema()` alone): the same tools, underived —
            # nothing is scoped or deferred, exactly as the old loop offered them.
            return [ToolSchema.from_openai(s) for s in self.registry.openai_schema()
                    if str(((s.get("function") or s) if isinstance(s, Mapping) else {})
                           .get("name") or "") not in self.exclude]
        # The ONE derivation of a surface's view (core/deferred/hidden), shared with the old
        # CLI chat loop (scoped_tools.surface_view) so the two paths cannot disagree.
        view = surface_view(self.registry, self.surface) if enabled() else None
        out: list = []
        for s in self.registry.specs():
            if s.name in self.exclude:
                continue
            standing = (view or {}).get(s.name, "core")
            if standing == "hidden":
                continue
            out.append(ToolSchema(name=s.name, description=s.description,
                                  parameters=dict(s.parameters), kind=s.kind,
                                  reversibility=getattr(s, "reversibility", "safe"),
                                  deferred=standing == "deferred"))
        return out

    def policy(self, call: ToolCall, args: dict, state: RunState) -> Optional[dict]:
        """THE ORDERED TOOL POLICY for BOSS's own tools (BOSSAPP-815, server ``boss``): the same
        tool_policy_engine.decide the MCP gateway and the receipt door ask. Only a MATCHED rule
        changes anything here — no rule, and the call goes on exactly as before (this tool set's
        own write hold and identity gate still apply after an allow). None when there is no
        database to read rules from or no rule decided."""
        from sqlalchemy.engine import Engine
        from sqlalchemy.exc import ProgrammingError

        engine = getattr(self.ctx, "engine", None)
        if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
            return None  # rules live in Postgres; a test double or SQLite store holds none
        from ..services.tool_policy_engine import BOSS_SERVER, PolicyContext, decide

        kind = "read"
        try:
            kind = getattr(self.registry.get(call.name), "kind", "read")
        except Exception:  # noqa: BLE001 — an unknown tool is refused further on
            pass
        ctx = PolicyContext(server=BOSS_SERVER, tool=call.name, arguments=args,
                            tier=2 if kind == "write" else 0, project=self.project_id,
                            run=getattr(state, "run_id", None) or None, role=self.role_key,
                            workflow=self.workflow_id, base="allow")
        try:
            with engine.begin() as connection:
                found = decide(ctx, connection=connection, record=True)
        except ProgrammingError as error:
            # A database whose schema predates the table holds no rule to skip; any other
            # failure is raised (and the call fails) rather than read as "no rule".
            if "tool_policies_v2" in str(error):
                return None
            raise
        return found if found.get("rule_id") else None

    def invoke(self, call: ToolCall, *, state: RunState,
               approved: Optional[dict] = None) -> ToolResult:
        args = dict(call.arguments or {})
        if call.name in self.exclude:
            return unknown(call)
        try:
            verdict = self.policy(call, args, state)
            if verdict is not None and verdict["decision"] == "deny":
                return ToolResult(call.id, call.name, "refused", {
                    "ok": False, "status": "refused", "error": "denied_by_policy",
                    "matched_policy_ids": verdict["matched_policy_ids"],
                    "message": f"'{call.name}' NOT run: {verdict['explanation']}"})
            if verdict is not None and verdict["decision"] == "ask" and approved is None:
                self.pending.append({"tool": call.name, "args": args,
                                     "policy": verdict["matched_policy_ids"]})
                return ToolResult(call.id, call.name, "ok", {
                    "ok": False, "status": "confirmation_required",
                    "note": f"'{call.name}' NOT run: a tool policy asks a person first. "
                            f"{verdict['explanation']}",
                    "proposed": {"tool": call.name, "args": args}})
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
