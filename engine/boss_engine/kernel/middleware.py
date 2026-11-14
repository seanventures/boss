"""THE SIX-HOOK MIDDLEWARE CONTRACT on the kernel loop (AP-P1b, BOSSAPP-1052).

docs/design/AGENT_MIDDLEWARE.md is the contract; docs/design/AGENT_PLATFORM.md §4.2 the design.
LangChain v1 shrank "an agent" to one loop with six interception points. The kernel was already
one loop driven by five internal policy protocols (policies.py); this module makes those six
points the PUBLIC extension surface, and re-expresses the five policies as built-in middleware on
them, so a cross-cutting concern (budget, authz, screening, receipts, memory, retries) is written
once and reaches every preset.

    before_agent(run)                      node   once, as a run starts (and on resume)
    before_model(run, call)                node   before every model call
    wrap_model_call(run, call, handler)    wrap   around every model call
    after_model(run, turn)                 node   after every model call
    wrap_tool_call(run, request, handler)  wrap   around every tool call
    after_agent(run, result)               node   once, as the run ends or suspends

THE STACK IS [verify] + [the spec's middleware ...] + [the preset's other policies] + [GOVERNANCE].

  * Node hooks run in stack order before (before_agent, before_model) and in REVERSE after
    (after_model, after_agent). Wrap hooks nest onion-style, the first entry outermost.
  * So GOVERNANCE, which the kernel appends last, is always NEAREST THE EFFECT: its wrap hooks are
    the innermost layer around the engine and the toolset (every attempt a retry makes is
    reserved and receipted; no spec middleware can rewrite a call's arguments after authz decided
    them, or hand the model a tool result the screen did not see), its before_* hooks run last
    and its after_* hooks first.
  * Governance is MANDATORY: `build_chain` always appends it, and a spec that names a governance
    or policy middleware of its own is refused (MiddlewareError) — there is no switch.

A node hook returns None (go on) or a `Jump`: `end` (stop, with the outcome), `model` (ask the
model again: a re-ask or an empty-completion press), or `tools` (run the turn's tool calls — what
after_model does when it says nothing). A middleware declares the jumps it may take in
`can_jump_to`; an undeclared one is a programming error. The first jump a phase returns wins.

A wrap hook receives `handler`, the next layer inward, and may call it zero times (a refusal, a
hold, a cached answer), once, or several times (retry, fallback). The model handler returns a
`step_model.Called` (a turn, or the recovery's verdict: go round / end); the tool handler a
ToolResult. `call.override(...)` / `request.override(...)` is a TRANSIENT change for this call
only; a persistent one is written to `run.state` (RunState.extra under the middleware's own key),
which the checkpoint carries — a middleware keeps no process-local state between hooks.

An exception escaping a node hook ends the run `failed`, naming the middleware; one escaping the
whole model chain is classified like an engine error (errors.classify) and ends it `failed`; one
escaping the whole tool chain is the failed call's result the model reads (toolset.failed).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional, Sequence

from .engine import ModelRequest
from .types import ControllerState, Step, StopReason, ToolCall, ToolResult, ToolSchema

JumpTo = Literal["end", "model", "tools"]
HOOKS = ("before_agent", "before_model", "wrap_model_call", "after_model", "wrap_tool_call",
         "after_agent")


class MiddlewareError(ValueError):
    """A stack that breaks the contract: a spec removing or shadowing governance, a jump a
    middleware did not declare. Raised before the run starts (or at the hook that broke it)."""


@dataclass(frozen=True)
class Jump:
    """A node hook's instruction to the loop. `end` carries the outcome the run ends with."""

    to: JumpTo
    status: str = "completed"
    stopped: str = ""
    error: str = ""
    text: Optional[str] = None
    capped: bool = False
    pending: Optional[dict] = None


@dataclass
class ModelCall:
    """One model call as before_model and wrap_model_call see it: whether this is the forced
    answer (and why), the tools offered, and the request (built by the `context` middleware).
    `notes` is shared by every copy `override` makes (the attempt number, a latency)."""

    answering: bool = False
    reason: Optional[StopReason] = None
    offered: list = field(default_factory=list)
    request: Optional[ModelRequest] = None
    notes: dict = field(default_factory=dict)

    def override(self, **changes: Any) -> "ModelCall":
        """A copy whose REQUEST has `changes` (system, messages, tools, tool_choice, ...): a
        transient change for this call only. The run's state is untouched."""
        request = dataclasses.replace(self.request, **changes) if changes else self.request
        return dataclasses.replace(self, request=request)


@dataclass
class ToolRequest:
    """One tool call as wrap_tool_call sees it. `schema` is the tool as offered this step (None:
    not offered); `approved` the person's approval when a resume runs a held call; `notes` is
    shared by every copy (`wrote`: a write ran)."""

    call: ToolCall
    schema: Optional[ToolSchema]
    step: int
    approved: Optional[dict] = None
    notes: dict = field(default_factory=dict)

    def override(self, **arguments: Any) -> "ToolRequest":
        """A copy with these arguments merged into the call's: transient, this call only."""
        import json

        args = dict(self.call.arguments or {})
        args.update(arguments)
        call = dataclasses.replace(self.call, arguments=args, raw_arguments=json.dumps(args))
        return dataclasses.replace(self, call=call)


class Middleware:
    """A middleware: any subset of the six hooks (a missing one passes through), a `name` and a
    `version` (recorded on run.started as `name@version`), and the jumps it may take."""

    name: str = "middleware"
    version: str = "1"
    can_jump_to: tuple = ()


# ── The chain ─────────────────────────────────────────────────────────────────────────────────

def _has(middleware: Any, hook: str) -> bool:
    fn = getattr(middleware, hook, None)
    if not callable(fn):
        return False
    base = getattr(Middleware, hook, None)
    return getattr(type(middleware), hook, None) is not base


class Chain:
    """The composed stack. Built once per drive (`build_chain`)."""

    def __init__(self, stack: Sequence[Any], *, mandatory: Iterable[str] = ()):
        self.stack = list(stack)
        self.mandatory = frozenset(mandatory)
        self._by_hook = {hook: [m for m in self.stack if _has(m, hook)] for hook in HOOKS}

    # ── introspection ─────────────────────────────────────────────────────────────────────
    def names(self) -> list:
        return [str(getattr(m, "name", type(m).__name__)) for m in self.stack]

    def versions(self) -> dict:
        return {str(getattr(m, "name", type(m).__name__)): str(getattr(m, "version", "1"))
                for m in self.stack}

    def get(self, name: str) -> Any:
        return next((m for m in self.stack if getattr(m, "name", None) == name), None)

    # ── node hooks ────────────────────────────────────────────────────────────────────────
    def _node(self, hook: str, run: Any, *args: Any, reverse: bool = False) -> Optional[Jump]:
        layers = self._by_hook[hook]
        for middleware in (reversed(layers) if reverse else layers):
            name = getattr(middleware, "name", type(middleware).__name__)
            try:
                jump = getattr(middleware, hook)(run, *args)
            except MiddlewareError:
                raise
            except Exception as exc:  # noqa: BLE001 — the contract: the run fails, named
                return Jump("end", status="failed", stopped="middleware",
                            error=f"middleware {name} ({hook}): {type(exc).__name__}: {exc}"[:600])
            if jump is None:
                continue
            if not isinstance(jump, Jump):
                raise MiddlewareError(f"middleware {name}: {hook} returned {jump!r}, not a Jump")
            if jump.to not in tuple(getattr(middleware, "can_jump_to", ()) or ()):
                raise MiddlewareError(f"middleware {name}: {hook} jumped to {jump.to!r}, which "
                                      f"it does not declare in can_jump_to")
            return jump
        return None

    def before_agent(self, run: Any) -> Optional[Jump]:
        return self._node("before_agent", run)

    def before_model(self, run: Any, call: ModelCall) -> Optional[Jump]:
        return self._node("before_model", run, call)

    def after_model(self, run: Any, turn: Any) -> Optional[Jump]:
        return self._node("after_model", run, turn, reverse=True)

    def after_agent(self, run: Any, result: Any) -> Any:
        for middleware in reversed(self._by_hook["after_agent"]):
            name = getattr(middleware, "name", type(middleware).__name__)
            try:
                out = middleware.after_agent(run, result)
            except Exception as exc:  # noqa: BLE001 — the result stands; the failure is logged
                import logging

                logging.getLogger("boss.kernel").warning(
                    "middleware %s (after_agent) failed (non-fatal): %s", name, exc)
                continue
            if out is not None:
                result = out
        return result

    # ── wrap hooks ────────────────────────────────────────────────────────────────────────
    def model_call(self, run: Any, call: ModelCall, inner: Callable[[ModelCall], Any]) -> Any:
        handler = inner
        for middleware in reversed(self._by_hook["wrap_model_call"]):
            handler = _bind(middleware.wrap_model_call, run, handler)
        try:
            return handler(call)
        except Exception as exc:  # noqa: BLE001 — classified like an engine error
            from .errors import classify
            from .step_model import Called

            error = classify(exc)
            return Called(next="end", status="failed", stopped=error.kind, error=error.detail)

    def tool_call(self, run: Any, request: ToolRequest,
                  inner: Callable[[ToolRequest], ToolResult]) -> ToolResult:
        handler = inner
        for middleware in reversed(self._by_hook["wrap_tool_call"]):
            handler = _bind(middleware.wrap_tool_call, run, handler)
        try:
            result = handler(request)
        except Exception as exc:  # noqa: BLE001 — a failed call is a result the model reads
            from .toolset import failed

            return failed(request.call, exc)
        if not isinstance(result, ToolResult):
            from .toolset import result_from

            result = result_from(request.call, result)
        return result


def _bind(wrap: Callable, run: Any, handler: Callable) -> Callable:
    return lambda value: wrap(run, value, handler)


# ── The five policies, as built-in middleware ─────────────────────────────────────────────────
#
# Each wraps the preset's policy object: the rules stay data in policies.py (so a preset reads as
# its rules), and these say WHERE in the run each rule acts.

class Limits(Middleware):
    """StopPolicy at `before_model`: stop now, answer now (no tools, the answer prompt), or go
    on (Caps: steps, spend, deadline, the turn budget)."""

    name = "limits"
    can_jump_to = ("end",)

    def __init__(self, stop: Any):
        self.stop = stop
        self.version = str(getattr(stop, "version", "1"))

    def before_model(self, run: Any, call: ModelCall) -> Optional[Jump]:
        state = run.state
        reason = self.stop.decide(state)
        if reason is not None and reason.kind == "stop":
            state.stopped = reason.why
            state.exhausted = state.exhausted or reason.why
            refused = state.extra.get("_refused_why")
            return Jump("end", status="failed" if refused else "completed", stopped=reason.why,
                        error=refused or "", capped=True)
        call.reason = reason
        call.answering = reason is not None
        state.answering = call.answering
        if call.answering:
            state.exhausted = state.exhausted or reason.why
            prompt = self.stop.answer_prompt(state, reason)
            if prompt:
                state.messages.append({"role": "user", "content": prompt})
                state.answer_prompted = True
        return None


class Context(Middleware):
    """ToolPolicy (which tools this step offers) and ContextPolicy (the request) at
    `before_model`: the request is assembled here, so a later before_model hook or any
    wrap_model_call reads it and may override it for the call."""

    name = "context"

    def __init__(self, context: Any, tools: Any, stop: Any):
        self.context, self.tools, self.stop = context, tools, stop
        self.version = f"{getattr(context, 'version', '1')}+{getattr(tools, 'version', '1')}"

    def before_model(self, run: Any, call: ModelCall) -> Optional[Jump]:
        state = run.state
        available = run.toolset.schemas(step=state.step, state=state)
        if call.answering:
            call.offered = self.tools.answer_tools(state, call.reason, available)
            if getattr(self.stop, "answer", "") == "extra" and call.reason.why == "steps":
                state.extra["_extra_answered"] = True
        else:
            call.offered = self.tools.select(state, available)
        call.request = self.context.assemble(state, call.offered)
        return None


class Verify(Middleware):
    """VerifyPolicy at `after_model`: a step's findings, and a final text accepted (the run
    ends) or asked again (a jump to the model)."""

    name = "verify"
    can_jump_to = ("end", "model")

    def __init__(self, verify: Any):
        self.verify = verify
        self.version = str(getattr(verify, "version", "1"))

    def after_model(self, run: Any, turn: Any) -> Optional[Jump]:
        state = run.state
        if turn.tool_calls:
            if not state.answering:
                for finding in self.verify.after_step(
                        state, Step(state.step, turn, [], ControllerState.CONTINUE)):
                    state.findings.append(finding)
            return None
        text = turn.text or ""
        verdict = self.verify.accept_final(state, text)
        run.rec.emit("verify.result", {"command": "accept_final", "exit": 0 if verdict.ok else 1,
                                       "passed": int(verdict.ok), "failed": int(not verdict.ok)})
        if verdict.ok:
            state.structured = verdict.structured
            run.steps.append(Step(state.step, turn, [], ControllerState.STOP))
            return Jump("end", status="completed", text=text, capped=state.answering)
        state.extra["_refused_why"] = verdict.why
        if state.reasks < self.verify.max_reasks:
            state.reasks += 1
            state.messages.append({"role": "user", "content": self.verify.reask(verdict.why)})
            run.steps.append(Step(state.step, turn, [], ControllerState.CONTINUE))
            return Jump("model")
        return Jump("end", status="failed", stopped="refused", error=verdict.why)


class Recovery(Middleware):
    """RecoveryPolicy at `wrap_model_call` (retry a hiccup, feed back a refused call, wait or
    park on the budget, answer at the deadline, fail) and at `after_model` (an empty completion
    is pressed again). Its tool side (the same-tool nudge) acts as each call is answered
    (step_tools), because the nudge follows the step's tool messages."""

    name = "recovery"
    can_jump_to = ("model",)

    def __init__(self, recovery: Any):
        self.recovery = recovery
        self.version = str(getattr(recovery, "version", "1"))

    def wrap_model_call(self, run: Any, call: ModelCall, handler: Callable) -> Any:
        from .step_model import recover

        return recover(run, call, handler)

    def after_model(self, run: Any, turn: Any) -> Optional[Jump]:
        state = run.state
        if turn.tool_calls or (turn.text or "").strip():
            return None
        prompt = self.recovery.on_empty(state)
        if not prompt:
            return None
        state.empties += 1
        state.messages.append({"role": "user", "content": prompt})
        run.rec.emit("model.retry", {"why": "empty completion", "attempt": state.empties})
        run.steps.append(Step(state.step, turn, [], ControllerState.CONTINUE))
        return Jump("model")


class ToolArgs(Middleware):
    """The kernel's own check of a call at `wrap_tool_call`: a tool not offered this step is
    refused naming that; arguments that break the tool's schema are refused with the JSON pointer
    of the fault, and the model may write the call again (RecoveryPolicy.on_invalid). A call a
    person approved (resume) was bound to its arguments by that approval and is not re-checked."""

    name = "tool_args"

    def __init__(self, recovery: Any):
        self.recovery = recovery

    def wrap_tool_call(self, run: Any, request: ToolRequest, handler: Callable) -> ToolResult:
        if request.approved is not None:
            return handler(request)
        from .schema_check import check_arguments
        from .step_tools import invalid
        from .toolset import unknown

        call, schema, state = request.call, request.schema, run.state
        if schema is None:
            return unknown(call)
        why = check_arguments(call.arguments, schema.parameters)
        if why:
            state.invalid[call.name] = int(state.invalid.get(call.name) or 0) + 1
            verdict = self.recovery.on_invalid(state, why, state.invalid[call.name])
            run.rec.emit("schema.invalid", {"by": "kernel", "tool": call.name, "where": why,
                                            "attempt": state.invalid[call.name],
                                            "verdict": verdict})
            return invalid(call, why, fail=verdict == "fail")
        if state.invalid.get(call.name):
            run.rec.emit("schema.repaired", {"tool": call.name})
            state.invalid[call.name] = 0
        return handler(request)


#: The policy middleware. `verify` is the OUTERMOST layer of the stack (its after_model runs
#: last, so a spec's own after_model sees a final answer — and may ask again — before it is
#: accepted); the rest sit between the spec's middleware and governance: before_model runs
#: limits → context, after_model (reversed) recovery's empty press, tool calls pass tool_args.
POLICY_NAMES = ("verify", "limits", "context", "recovery", "tool_args")


def policy_middleware(policies: Any) -> list:
    return [Verify(policies.verify), Limits(policies.stop),
            Context(policies.context, policies.tools, policies.stop),
            Recovery(policies.recovery), ToolArgs(policies.recovery)]


def build_chain(spec: Any) -> Chain:
    """The stack a run drives: [verify] + the spec's middleware + the preset's other policies +
    governance — always, and last (nearest the effect). A spec that names a governance or policy
    middleware of its own is refused: it can add behaviour, never remove or replace a gate."""
    from .governance import GOVERNANCE_NAMES, governance_middleware

    user = list(getattr(spec, "middleware", None) or ())
    reserved = set(GOVERNANCE_NAMES) | set(POLICY_NAMES)
    seen: set = set()
    for middleware in user:
        name = str(getattr(middleware, "name", "") or "")
        if not name:
            raise MiddlewareError(f"{middleware!r} has no name")
        if name in reserved:
            raise MiddlewareError(
                f"middleware {name!r} is built in (governance and the preset's policies are "
                "always present and cannot be replaced or removed by a spec)")
        if name in seen:
            raise MiddlewareError(f"middleware {name!r} appears twice")
        seen.add(name)
        bad = set(getattr(middleware, "can_jump_to", ()) or ()) - {"end", "model", "tools"}
        if bad:
            raise MiddlewareError(f"middleware {name!r} declares unknown jumps {sorted(bad)}")
    verify, *policies = policy_middleware(spec.policies)
    return Chain([verify] + user + policies + governance_middleware(spec),
                 mandatory=GOVERNANCE_NAMES)


__all__ = ["Chain", "Context", "HOOKS", "Jump", "Limits", "Middleware", "MiddlewareError",
           "ModelCall", "POLICY_NAMES", "Recovery", "ToolArgs", "ToolRequest", "Verify",
           "build_chain", "policy_middleware"]
