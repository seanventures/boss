"""THE ONE LOOP: model → typed tool calls → results → model, until the middleware says stop.

Six loops in BOSS did this, each with its own caps, final-answer rule, error handling and usage
sums (inventory §1.1). This is the one they move onto (design §2.6, §8). The loop decides
nothing on its own: every rule is a policy (policies.py), and every policy acts at one of the
six middleware hooks (middleware.py, docs/design/AGENT_MIDDLEWARE.md), with governance appended
to every stack and nearest the effect (governance.py). Each step:

    before_model                 limits (StopPolicy: stop now, answer now, or go on), the spec's
                                 own hooks, context (ToolPolicy + ContextPolicy: the request)
    wrap_model_call              the spec's wraps, recovery (RecoveryPolicy's retries, waits and
                                 feedback), telemetry + budget (receipt, reservation), the engine
    after_model                  telemetry (model.response), recovery (an empty completion is
                                 pressed again), verify (VerifyPolicy accepts the answer or asks
                                 again), the spec's own hooks
    wrap_tool_call (each call)   the spec's wraps, tool_args (offered? schema?), authz, the
                                 screen, audit, the ToolSet (step_tools)

and the controller state of the step is recorded: CONTINUE, ANSWER, SUSPEND or STOP.
`before_agent` runs once as a run starts or resumes, `after_agent` once as it ends or suspends.

`run(spec)` starts a run; `resume(spec, decision=...)` continues a suspended one from its
checkpoint (a person's approval, a typed answer, or the budget freeing); `cancel(run_id, by=...)`
stops a live one before its next model or tool call.
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Optional

from .checkpoint import DEFAULT_STORE
from .engine import CancelToken
from .governance import approval_refusal
from .ledger import SinkMeta
from .ledger_tool import LedgerTool
from .middleware import Jump, ModelCall, ToolRequest, build_chain
from .policies import system_blocks
from .recorder import Recorder
from .spec import RunSpec
from .state import RunState, repair_orphans
from .step_model import Called, complete
from .step_tools import run_calls
from .toolset import Composite
from .types import ControllerState, KernelResult, Step, ToolCall, ToolResult

_LIVE: dict = {}
_LIVE_LOCK = threading.Lock()


class _Run:
    """One drive of a run: the spec, its state, its recorder, its middleware chain (the spec's,
    the preset's policies, governance), and the tools (the built-in ledger tool joined)."""

    def __init__(self, spec: RunSpec, state: RunState):
        self.spec = spec
        self.state = state
        self.policies = spec.policies
        self.chain = build_chain(spec)
        self.engine = spec.engine
        self.last_latency_ms = 0
        self.toolset = (Composite(spec.tools, LedgerTool(spec.ledger_reader))
                        if spec.ledger_reader is not None else spec.tools)
        self.store = spec.store if spec.store is not None else DEFAULT_STORE
        self.token = spec.cancel or CancelToken(clock=spec.clock)
        self.rec = Recorder(state, sink=spec.sink, on_event=spec.on_event,
                            policy={**self.policies.versions(),
                                    "middleware": ",".join(
                                        f"{n}@{v}" for n, v in self.chain.versions().items())},
                            meta=SinkMeta(engine=str(getattr(spec.engine, "name", "") or ""),
                                          stage=spec.stage, scaffold_level=spec.scaffold_level,
                                          actor=spec.actor))
        self.steps: list = []

    def cancelled(self) -> bool:
        return self.token.is_set()

    # ── the loop ──────────────────────────────────────────────────────────────────────────
    def drive(self) -> KernelResult:
        state = self.state
        with _LIVE_LOCK:
            _LIVE[state.run_id] = self.token
        try:
            while True:
                if self.cancelled():
                    return self.end("cancelled", stopped="cancelled")
                ended = self.step()
                if ended is not None:
                    return ended
                if self.spec.persist:
                    self.store.save(state)
        finally:
            with _LIVE_LOCK:
                _LIVE.pop(state.run_id, None)

    def step(self) -> Optional[KernelResult]:
        state = self.state
        call = ModelCall()
        jump = self.chain.before_model(self, call)
        if jump is not None:
            return self.jump(jump)
        called = self.chain.model_call(self, call, self.complete)
        if called.next == "continue":
            return None
        if called.next == "end":
            return self.end(called.status, stopped=called.stopped, error=called.error,
                            pending={"kind": "budget_wait", "budget": called.budget}
                            if called.budget else None)
        turn = called.turn
        state.step += 1
        state.add_usage(turn.usage)
        state.messages.append(turn.raw)
        self.last_latency_ms = called.latency_ms
        jump = self.chain.after_model(self, turn)
        if jump is not None and jump.to != "tools":
            return self.jump(jump)
        if turn.tool_calls and call.answering:
            return self.closing_offer(turn, call.offered)
        if turn.tool_calls:
            ended = run_calls(self, turn.tool_calls, call.offered)
            control = ControllerState.SUSPEND if ended else ControllerState.CONTINUE
            self.steps.append(Step(state.step, turn, [], control))
            if ended is not None:
                return self.end(ended["status"], pending=ended["pending"],
                                stopped=ended["status"])
            return None
        # A final text no verify layer ended or re-asked (verify is always in the stack, so this
        # is a stack without one): it is the answer.
        self.steps.append(Step(state.step, turn, [], ControllerState.STOP))
        return self.end("completed", text=turn.text or "", capped=state.answering)

    def complete(self, call: ModelCall) -> Called:
        """The model node: the innermost handler of the wrap_model_call chain."""
        return complete(self, call)

    def invoke_tool(self, request: ToolRequest) -> ToolResult:
        """The tool node: the innermost handler of the wrap_tool_call chain."""
        return self.toolset.invoke(request.call, state=self.state, approved=request.approved)

    def jump(self, jump: Jump) -> Optional[KernelResult]:
        if jump.to == "end":
            return self.end(jump.status, text=jump.text, stopped=jump.stopped, error=jump.error,
                            pending=jump.pending, capped=jump.capped)
        return None  # `model`: go round and ask again

    def closing_offer(self, turn: Any, offered: list) -> KernelResult:
        """An answer forced by the budget may attach the fix it found (EngineAgent's closing
        propose_action): the one offered tool runs, nothing else does, and no further call is
        made — the text is the answer."""
        state = self.state
        state.extra["_offer_spent"] = True
        names = {t.name for t in offered}
        calls = [c for c in turn.tool_calls if c.name in names][:1]
        if calls:
            run_calls(self, calls, offered)
        return self.end("completed", text=turn.text or "", capped=True)

    # ── endings ───────────────────────────────────────────────────────────────────────────
    def end(self, status: str, *, text: Optional[str] = None, stopped: str = "",
            error: str = "", pending: Optional[dict] = None, capped: bool = False
            ) -> KernelResult:
        state, rec = self.state, self.rec
        if text is not None:
            state.final_text = text
        if capped and not (state.final_text or "").strip():
            state.final_text = getattr(self.policies.stop, "fallback_text", "") or ""
        state.stopped = stopped or state.stopped
        state.pending = pending
        state.status = {"suspended": "awaiting_approval"}.get(status, status)
        kind = {"completed": "run.completed", "failed": "run.failed",
                "cancelled": "run.cancelled"}.get(status, "run.suspended")
        if status == "budget_wait":
            rec.emit("budget.wait", {"mode": "durable", **(pending or {})})
        rec.emit(kind, {"status": state.status, "stopped": state.stopped, "error": error[:600],
                        "steps": state.step})
        if status in ("awaiting_approval", "budget_wait", "cancelled") or self.spec.persist:
            self.store.save(state)
        result = KernelResult(
            status="suspended" if state.status == "awaiting_approval" else state.status,
            run_id=state.run_id, text=state.final_text or "", structured=state.structured,
            findings=list(state.findings), pending=state.pending, usage=state.usage,
            steps=state.step, exhausted=state.exhausted, stopped=state.stopped, error=error,
            trace=list(state.trace), events=list(rec.events))
        return self.chain.after_agent(self, result)


# ── The API ───────────────────────────────────────────────────────────────────────────────────

def _new_state(spec: RunSpec) -> RunState:
    run_id = spec.run_id or f"krun_{uuid.uuid4().hex[:20]}"
    return RunState(run_id=run_id, project_id=spec.project_id,
                    lineage_id=spec.lineage_id or run_id, attempt=int(spec.attempt or 1),
                    act_id=spec.act_id, system=system_blocks(spec.system),
                    messages=[dict(m) for m in spec.messages], extra=dict(spec.extra))


def run(spec: RunSpec) -> KernelResult:
    """Start a run and drive it until it completes, fails, suspends or is cancelled."""
    state = _new_state(spec)
    drive = _Run(spec, state)
    drive.rec.emit("run.started", {"actor": spec.actor, "stage": spec.stage,
                                   "scaffold_level": spec.scaffold_level,
                                   "lineage_id": state.lineage_id, "attempt": state.attempt,
                                   "middleware": drive.chain.names()})
    if spec.persist:
        drive.store.save(state)
    jump = drive.chain.before_agent(drive)
    if jump is not None and jump.to == "end":
        return drive.jump(jump)
    return drive.drive()


def resume(spec: RunSpec, *, decision: Optional[dict] = None) -> KernelResult:
    """Continue the suspended run `spec.run_id` from its checkpoint.

    Waiting on a person's APPROVAL: `decision` must approve the held call ({"approved": True,
    "decision_id": ...}, plus the approval's binding — `args_sha256`, and on a v2 decision
    `binding_version`, `args_preview` and `args_sig` — when it bound the arguments; the authz
    governance's `approval_refusal` decides). The held call then runs down the tool chain WITHOUT
    asking its gates again — the approval is the answer to them — its result replaces the
    deferral the model saw, and the loop goes on. Anything else is `denied` and the run stays
    suspended.

    Waiting on a person's ANSWER (a typed question: the pending carries `question: true`):
    `decision` is {"answered": True, "decision_id": ...} and the caller has already put the
    answer into the conversation the store holds; nothing is executed and the loop goes on.

    Waiting on the budget: the loop simply goes on.

    The ToolSet, engine and policies are the caller's to rebuild (a checkpoint holds data, never
    live objects), as resume_agent_run rebuilds its adapter and tools today."""
    store = spec.store if spec.store is not None else DEFAULT_STORE
    state = store.load(spec.run_id or "")
    if state is None:
        return KernelResult(status="failed", run_id=spec.run_id or "", error="no such run")
    drive = _Run(spec, state)
    if state.status == "awaiting_approval":
        pending = state.pending or {}
        if pending.get("question") and (decision or {}).get("answered"):
            if str((decision or {}).get("decision_id") or "") != str(
                    pending.get("decision_id") or ""):
                return KernelResult(status="denied", run_id=state.run_id, pending=state.pending,
                                    error="that answer is not to the question this run asked",
                                    usage=state.usage, steps=state.step)
        else:
            refusal = approval_refusal(pending, decision)
            if refusal:
                return KernelResult(status="denied", run_id=state.run_id, pending=state.pending,
                                    error=refusal, usage=state.usage, steps=state.step)
            _run_approved(drive, decision or {})
    elif state.status != "budget_wait":
        return KernelResult(status=state.status if state.status in (
            "completed", "failed", "cancelled") else "failed", run_id=state.run_id,
            text=state.final_text or "", usage=state.usage, steps=state.step,
            error="" if state.status in ("completed", "failed", "cancelled")
            else f"cannot resume a run that is {state.status}")
    state.status, state.pending = "running", None
    repaired = repair_orphans(state.messages)
    drive.rec.emit("run.resumed", {"decision_id": (decision or {}).get("decision_id"),
                                   "orphans_repaired": repaired})
    jump = drive.chain.before_agent(drive)
    if jump is not None and jump.to == "end":
        return drive.jump(jump)
    return drive.drive()


def _run_approved(drive: _Run, decision: dict) -> None:
    """The held call a person approved, down the same tool chain as any call (authz does not
    ask again, audit never runs it twice, the screen still reads its result)."""
    state, spec = drive.state, drive.spec
    pending = dict(state.pending or {})
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    call = ToolCall(id=str(pending.get("call_id") or ""), name=str(pending.get("tool") or ""),
                    arguments=dict(args), raw_arguments=json.dumps(args))
    step = int(pending.get("step") or 0)
    key = state.idempotency_key(step, call.id)
    schema = next((s for s in drive.toolset.schemas(step=state.step, state=state)
                   if s.name == call.name), None)
    result = drive.chain.tool_call(drive, ToolRequest(call=call, schema=schema, step=step,
                                                      approved=decision), drive.invoke_tool)
    body = drive.policies.context.render(result)
    for message in reversed(state.messages):
        if message.get("role") == "tool" and str(message.get("tool_call_id")) == call.id:
            message["content"] = body
            break
    state.rendered.append(body)
    state.trace.append({"step": step, "tool": call.name, "args": args,
                        "status": result.status, "result": result.content, "call_id": call.id,
                        "resumed": True})
    if spec.resume_note is not None:
        state.messages.append({"role": "user", "content": spec.resume_note(pending, body)})
    drive.rec.emit("tool.result", {"tool": call.name, "call_id": call.id,
                                   "status": result.status, "args": args,
                                   "decision_id": pending.get("decision_id"), "approved": True,
                                   "idempotency_key": key})


def cancel(run_id: str, *, by: str, reason: str = "cancelled") -> bool:
    """Stop a live run before its next model or tool call. False when no such run is live in
    this process (a run on another worker is stopped through its own CancelToken's
    `should_stop`, e.g. code_task_stop_requests)."""
    with _LIVE_LOCK:
        token = _LIVE.get(run_id)
    if token is None:
        return False
    token.cancel(by=by, reason=reason)
    return True


__all__ = ["cancel", "resume", "run"]
