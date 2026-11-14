"""GOVERNANCE AS MANDATORY MIDDLEWARE: every model call reserved, receipted and settled, every
tool call decided, held on a Decision, screened and audited — by the kernel, for every preset.

The governance middleware (AP-P1b, plus AP-P4's trace; docs/design/AGENT_PLATFORM.md §4.10,
AGENT_MIDDLEWARE.md) are appended to every run's stack by `middleware.build_chain`, last, so they sit nearest the
effect; a spec cannot remove, replace or reorder them:

  trace       every hook                     the run's spans (kernel/trace.py, AP-P4): run, step,
                                             model, tool, subagent, interrupt, handoff
  telemetry   wrap_model_call + after_model  model.request / model.response events: through the
                                             ledger sink these are the run's `llm_calls` rows
                                             (kernel/ledger.DbSink -> record_llm_call)
  budget      wrap_model_call                the budget gate: each attempt reserved before the
                                             engine and settled (or released) after it
  authz       wrap_tool_call                 the permission/identity gate (run token), and a
                                             held call must be a Decision bound to its arguments;
                                             `approval_refusal` is the resume-side check
  tool_screen wrap_tool_call                 an outside result screened (tool_result_screen),
                                             marked and remembered (low_trust)
  audit       wrap_tool_call                 a write's idempotency key (never run twice), its
                                             latency, the step's "a write ran"

Before this, governance was applied per call site: agent_loop and step_recovery wrap their
adapter in `model_routing.governed`, the router's answer agent and meetings do not,
NativeCodeAgent and TypeSafeDecider build adapters that nothing governs (inventory §1.2). A run
on the kernel cannot forget: `RunSpec.gate` is applied here, by the budget middleware, around
every call (design §2.3).

The gate is BOSS's existing one (GovernedChatAdapter's reserve / settle / release): the budget,
the receipt and the burst cap are decided by exactly the code that decides them now. A refusal
surfaces as ModelSpendRefused (the run stops: a person must lift the cap) and a burst deferral as
ModelSpendDeferred (the RecoveryPolicy waits or parks the run).
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from .engine import CancelToken, ModelRequest
from .types import ModelTurn


class BudgetGate(Protocol):
    def open(self) -> Any: ...

    def settle(self, held: Any, usage: dict) -> None: ...

    def fail(self, held: Any, error: BaseException) -> None: ...


class GovernedEngine:
    """`engine` with every `complete` reserved before and settled after (released on failure).
    Retries happen OUTSIDE it, so each attempt is its own reservation and receipt: a retried
    call is visible cost, not a hidden one."""

    def __init__(self, engine: Any, gate: BudgetGate):
        self.inner = engine
        self.gate = gate
        self.name = getattr(engine, "name", "engine")
        self.traits = getattr(engine, "traits", None)

    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn:
        held = self.gate.open()
        try:
            turn = self.inner.complete(req, on_delta=on_delta, cancel=cancel)
        except BaseException as error:
            self.gate.fail(held, error)
            raise
        self.gate.settle(held, turn.usage.as_adapter_usage())
        return turn

    def count(self, req: ModelRequest) -> int:
        return self.inner.count(req)


class AdapterGate:
    """A GovernedChatAdapter (model_routing.governed) used as a gate only."""

    def __init__(self, governed_adapter: Any):
        self.governed = governed_adapter

    def open(self) -> Any:
        return self.governed.reserve_turn()

    def settle(self, held: Any, usage: dict) -> None:
        self.governed.settle_turn(held, usage)

    def fail(self, held: Any, error: BaseException) -> None:
        self.governed.release_turn(held, error)


def project_gate(connection: Any, settings: Any, project_id: str, *, skill_id: str, ref: str,
                 adapter: Any = None) -> AdapterGate:
    """The project's budget gate for a run: the same reservation size, category and receipts as
    `model_routing.governed(adapter, ...)`. `adapter` is the routed adapter the engine wraps (its
    source decides what a turn reserves)."""
    from ..services.model_routing import governed

    return AdapterGate(governed(adapter, connection, settings, project_id,
                                skill_id=skill_id, ref=ref))


# ── The governance middleware (mandatory; appended to every stack by build_chain) ──────────────

class Telemetry:
    """The receipt of every model call: `model.request` as each attempt goes out (inside the
    recovery's retries, so a retried call is a visible call) and `model.response` once the turn
    is back, with its usage and latency. The ledger sink turns these into the run's llm_calls
    rows and run_events (kernel/ledger.DbSink); without a sink they ride on the result."""

    name = "telemetry"
    version = "telemetry@1"
    can_jump_to: tuple = ()

    def wrap_model_call(self, run: Any, call: Any, handler: Callable) -> Any:
        request = call.request
        run.rec.emit("model.request", {"attempt": int(call.notes.get("attempt") or 1),
                                       "tools": [t.name for t in request.tools],
                                       "answering": call.answering,
                                       "messages": len(request.messages)})
        return handler(call)

    def after_model(self, run: Any, turn: Any) -> None:
        run.rec.emit("model.response", {"text": turn.text[:2000],
                                        "tool_calls": [c.name for c in turn.tool_calls],
                                        "stop_reason": turn.stop_reason},
                     usage=turn.usage, latency_ms=int(getattr(run, "last_latency_ms", 0) or 0))
        return None


class Budget:
    """The run's budget gate (RunSpec.gate: BOSS's GovernedChatAdapter reserve / settle /
    release) around EVERY attempt of every model call: inside the recovery's retries, so each
    attempt is its own reservation and receipt. A refusal raises ModelSpendRefused (the run
    stops), a burst deferral ModelSpendDeferred (the RecoveryPolicy waits or parks). With no
    gate the caller's adapter governs itself (a `governed(...)` adapter handed in)."""

    name = "budget"
    version = "budget@1"
    can_jump_to: tuple = ()

    def wrap_model_call(self, run: Any, call: Any, handler: Callable) -> Any:
        gate = run.spec.gate
        if gate is None:
            return handler(call)
        held = gate.open()
        try:
            called = handler(call)
        except BaseException as error:
            gate.fail(held, error)
            raise
        turn = getattr(called, "turn", None)
        gate.settle(held, turn.usage.as_adapter_usage() if turn is not None else {})
        return called


def identity_refusal(name: str, permission: Optional[str], project_id: Optional[str],
                     db: Any) -> Optional[dict]:
    """Inside a delegated run (a run token in context), the call is decided against the token
    before it runs — person ∩ ceilings ∩ grant ∩ task; an undeclared permission is refused (deny
    by default). Outside one: None, nothing to decide here (the registry's own gate decides)."""
    from ..identity import lineage
    from ..identity import runs as identity_runs

    ctx = lineage.current_context()
    if ctx is None or ctx.run_token is None:
        return None
    try:
        identity_runs.authorize_tool(permission, project_id, db=db)
    except identity_runs.ToolRefused as refused:
        return {"status": "refused", "error": "forbidden", "permission": refused.permission,
                "reason": refused.reason,
                "message": (f"'{name}' needs {refused.permission}, which this run's authority "
                            "does not include. Do not retry it; say what you could not do.")}
    return None


def approval_refusal(pending: Optional[dict], decision: Optional[dict]) -> str:
    """THE RESUME SIDE OF A HASH-BOUND APPROVAL: why `decision` does not release the call this
    run holds, or "" when it does. The decision must be approved, be the one the run waits on,
    and carry the binding (args_sha256; on a v2 decision binding_version + args_preview +
    args_sig) of these exact arguments — a v2 one fails closed without a valid signature
    (BOSSAPP-816)."""
    pending = pending or {}
    if not decision or not decision.get("approved"):
        return "the held call was not approved"
    if str(decision.get("decision_id") or "") != str(pending.get("decision_id") or ""):
        return "that decision is not the one this run waits on"
    from ..services.argument_binding import CHANGED, binding_holds

    if not binding_holds(str(pending.get("tool") or ""), pending.get("args"), decision):
        return CHANGED
    return ""


class Authz:
    """The permission/identity gate and the approval rule, around every tool call.

      * Inside a delegated run, the call's declared permission (ToolSchema.permission) is
        decided against the run token BEFORE the tool runs, and a catalogued set's call with no
        permission (toolset.UNDECLARED) is refused; a registry tool (GATED_BY_REGISTRY) is
        decided by the registry's own identity_gate instead, and a caller's own function
        outside the catalogue (permission None) by its toolset's gates.
        A call a person approved (resume) is not asked again: the approval answers it.
      * A call a tool HOLDS for a person must be held on a Decision: a deferral without a
        `decision_id` is refused (nothing ran, nothing waits on nobody), and the held call's
        argument digest is recorded with it, so `approval_refusal` can bind the resume to these
        exact arguments."""

    name = "authz"
    version = "authz@1"
    can_jump_to: tuple = ()

    def wrap_tool_call(self, run: Any, request: Any, handler: Callable) -> Any:
        from .toolset import GATED_BY_REGISTRY, UNDECLARED
        from .types import ToolResult

        call, schema = request.call, request.schema
        if request.approved is None:
            permission = getattr(schema, "permission", None) if schema is not None else None
            if permission is not None and permission != GATED_BY_REGISTRY:
                refusal = identity_refusal(call.name,
                                           None if permission == UNDECLARED else permission,
                                           run.state.project_id, run.spec.db)
                if refusal is not None:
                    run.rec.emit("escalation", {"tool": call.name, "why": "identity",
                                                "permission": refusal.get("permission")})
                    return ToolResult(call.id, call.name, "refused", refusal)
        result = handler(request)
        if result.status == "deferred" and result.budget is None:
            if not result.decision_id:
                return ToolResult(call.id, call.name, "refused", {
                    "ok": False, "status": "refused", "error": "hold_without_decision",
                    "message": (f"'{call.name}' was NOT run: it asked to wait for a person but "
                                "raised no decision for one to answer.")})
            from ..services.argument_binding import args_sha256

            args = call.arguments if isinstance(call.arguments, dict) else {}
            pending = dict(result.pending or {})
            pending.setdefault("args_sha256", args_sha256(call.name, args))
            result.pending = pending
        return result


class ToolScreen:
    """Tool-result screening and taint, around every tool call (BOSSAPP-822, low_trust).

    A result from a tool that names an outside ORIGIN (ToolSchema.origin) is screened — by the
    run's own screen (RunSpec.screen: the project's mode, the model check) or the deterministic
    pattern screen — unless it came through the receipt door, which screened it already. A
    result carrying the untrusted mark taints the run (`state.extra["tool_screen_untrusted"]`:
    the next write waits for a person), and an outside result's text is remembered in the run's
    low-trust ledger (`state.extra["untrusted_spans"]`), so a later call that would send it out
    can be held. Both ride in the checkpoint."""

    name = "tool_screen"
    version = "tool_screen@1"
    can_jump_to: tuple = ()

    _NOT_A_RESULT = ("deferred_for_approval", "skipped", "denied", "refused",
                     "awaiting_approval")

    def wrap_tool_call(self, run: Any, request: Any, handler: Callable) -> Any:
        result = handler(request)
        content = result.content
        if not isinstance(content, dict):
            return result
        from ..services.tool_result_screen import carried_untrusted, through_receipt_door

        schema, call, state = request.schema, request.call, run.state
        origin = getattr(schema, "origin", None) if schema is not None else None
        settled = content.get("status") in self._NOT_A_RESULT
        if origin and not settled and not getattr(schema, "prescreened", False) \
                and not through_receipt_door(content):
            content = self._screen(run, content, ref=f"{state.run_id}:{call.name}")
        untrusted = result.untrusted
        if carried_untrusted(content):
            state.extra["tool_screen_untrusted"] = True
            untrusted = True
        if origin and not settled:
            from ..services.low_trust import ORIGINS, Ledger, remember

            if origin not in ORIGINS:
                origin = "tool_result"  # an origin low_trust does not name: outside, generically

            ledger = Ledger.load(state.extra.get("untrusted_spans"))
            content = remember(content, origin=origin, ledger=ledger, echoed=call.arguments)
            state.extra["untrusted_spans"] = ledger.export()
        if content is result.content and untrusted == result.untrusted:
            return result
        import dataclasses

        return dataclasses.replace(result, content=content, untrusted=untrusted)

    @staticmethod
    def _screen(run: Any, content: dict, *, ref: str) -> Any:
        screen = getattr(run.spec, "screen", None)
        if screen is not None:
            screened, _verdict = screen(content, ref)
            return screened
        from ..services.tool_result_screen import apply_screen

        return apply_screen(content)


class Audit:
    """The record of a tool call that ran, around every call, innermost: a write whose
    idempotency key (run:step:call) already ran is answered "skipped" and never runs twice — a
    resume included; the call's latency; and the step's "a write ran" (workspace snapshot)."""

    name = "audit"
    version = "audit@1"
    can_jump_to: tuple = ()

    def wrap_tool_call(self, run: Any, request: Any, handler: Callable) -> Any:
        from .types import ToolResult

        state, call, schema = run.state, request.call, request.schema
        write = schema is not None and schema.kind == "write"
        key = state.idempotency_key(int(request.step), call.id)
        if (write or request.approved is not None) and key in state.executed:
            return ToolResult(call.id, call.name, "skipped",
                              {"status": "skipped", "note": "this exact call already ran"})
        started = run.spec.clock()
        result = handler(request)
        state.extra["_last_tool_latency"] = int((run.spec.clock() - started) * 1000)
        if request.approved is not None:
            state.executed.append(key)
        elif write and result.status == "ok":
            state.executed.append(key)
            request.notes["wrote"] = True
        return result


#: The mandatory stack, in order (outermost first for the wraps): the trace outermost (AP-P4:
#: a refused, held or screened call is still a span, and its after_model runs after the
#: receipt's, so the model span names an already-numbered llm_calls row); telemetry outside
#: budget so a refused reservation is still a requested call on record; authz outside the screen
#: and the audit so nothing reaches either undecided.
GOVERNANCE_NAMES = ("trace", "telemetry", "budget", "authz", "tool_screen", "audit")


def governance_middleware(spec: Any) -> list:
    from .trace import Trace

    return [Trace(), Telemetry(), Budget(), Authz(), ToolScreen(), Audit()]


__all__ = ["AdapterGate", "Audit", "Authz", "Budget", "BudgetGate", "GOVERNANCE_NAMES",
           "GovernedEngine", "Telemetry", "ToolScreen", "approval_refusal",
           "governance_middleware", "identity_refusal", "project_gate"]
