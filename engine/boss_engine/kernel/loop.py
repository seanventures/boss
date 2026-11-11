"""THE ONE LOOP: model → typed tool calls → results → model, until the policies say stop.

Six loops in BOSS did this, each with its own caps, final-answer rule, error handling and usage
sums (inventory §1.1). This is the one they move onto (design §2.6, §8): every rule that
differed is a policy field, so the loop below decides nothing on its own.

Each step:

    StopPolicy.decide            stop now, answer now (no tools), or go on
    ContextPolicy.assemble       system blocks, conversation, the step's tools
    Engine.complete              with RecoveryPolicy's retries, waits and feedback (step_model)
    tool calls                   checked, run or held, every call answered (step_tools)
    a final text                 empty? pressed again; VerifyPolicy accepts it or asks again

and the controller state of the step is recorded: CONTINUE, ANSWER, SUSPEND or STOP.

`run(spec)` starts a run; `resume(spec, decision=...)` continues a suspended one from its
checkpoint (a person's approval, or the budget freeing); `cancel(run_id, by=...)` stops a live
one before its next model or tool call.
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Optional

from .checkpoint import DEFAULT_STORE
from .engine import CancelToken
from .governance import GovernedEngine
from .ledger import SinkMeta
from .ledger_tool import LedgerTool
from .policies import system_blocks
from .recorder import Recorder
from .spec import RunSpec
from .state import RunState, repair_orphans
from .step_model import call_model
from .step_tools import run_calls
from .toolset import Composite, result_from
from .types import ControllerState, KernelResult, Step, StopReason, ToolCall

_LIVE: dict = {}
_LIVE_LOCK = threading.Lock()


class _Run:
    """One drive of a run: the spec, its state, its recorder, and the engine and tools as the
    kernel wraps them (governed; the built-in ledger tool joined)."""

    def __init__(self, spec: RunSpec, state: RunState):
        self.spec = spec
        self.state = state
        self.policies = spec.policies
        self.engine = GovernedEngine(spec.engine, spec.gate) if spec.gate is not None \
            else spec.engine
        self.toolset = (Composite(spec.tools, LedgerTool(spec.ledger_reader))
                        if spec.ledger_reader is not None else spec.tools)
        self.store = spec.store if spec.store is not None else DEFAULT_STORE
        self.token = spec.cancel or CancelToken(clock=spec.clock)
        self.rec = Recorder(state, sink=spec.sink, on_event=spec.on_event,
                            policy=self.policies.versions(),
                            meta=SinkMeta(engine=str(getattr(spec.engine, "name", "") or ""),
                                          stage=spec.stage, scaffold_level=spec.scaffold_level))
        self.steps: list = []

    def cancelled(self) -> bool:
        return self.token.is_set()

    # ── the loop ──────────────────────────────────────────────────────────────────────────
    def drive(self) -> KernelResult:
        state, pol = self.state, self.policies
        with _LIVE_LOCK:
            _LIVE[state.run_id] = self.token
        try:
            while True:
                if self.cancelled():
                    return self.end("cancelled", stopped="cancelled")
                reason = pol.stop.decide(state)
                if reason is not None and reason.kind == "stop":
                    state.stopped = reason.why
                    state.exhausted = state.exhausted or reason.why
                    return self.end("completed" if not state.extra.get("_refused_why")
                                    else "failed", stopped=reason.why,
                                    error=state.extra.get("_refused_why") or "",
                                    capped=True)
                ended = self.step(reason)
                if ended is not None:
                    return ended
                if self.spec.persist:
                    self.store.save(state)
        finally:
            with _LIVE_LOCK:
                _LIVE.pop(state.run_id, None)

    def step(self, reason: Optional[StopReason]) -> Optional[KernelResult]:
        state, pol, rec = self.state, self.policies, self.rec
        answering = reason is not None
        state.answering = answering
        available = self.toolset.schemas(step=state.step, state=state)
        if answering:
            state.exhausted = state.exhausted or reason.why
            prompt = pol.stop.answer_prompt(state, reason)
            if prompt:
                state.messages.append({"role": "user", "content": prompt})
                state.answer_prompted = True
            offered = pol.tools.answer_tools(state, reason, available)
            if getattr(pol.stop, "answer", "") == "extra" and reason.why == "steps":
                state.extra["_extra_answered"] = True
        else:
            offered = pol.tools.select(state, available)
        request = pol.context.assemble(state, offered)
        called = call_model(self, request, answering=answering)
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
        rec.emit("model.response", {"text": turn.text[:2000],
                                    "tool_calls": [c.name for c in turn.tool_calls],
                                    "stop_reason": turn.stop_reason},
                 usage=turn.usage, latency_ms=called.latency_ms)
        if turn.tool_calls and answering:
            return self.closing_offer(turn, offered)
        if turn.tool_calls:
            ended = run_calls(self, turn.tool_calls, offered)
            control = ControllerState.SUSPEND if ended else ControllerState.CONTINUE
            self.steps.append(Step(state.step, turn, [], control))
            for finding in pol.verify.after_step(state, self.steps[-1]):
                state.findings.append(finding)
            if ended is not None:
                return self.end(ended["status"], pending=ended["pending"],
                                stopped=ended["status"])
            return None
        return self.final_text(turn)

    def final_text(self, turn: Any) -> Optional[KernelResult]:
        state, pol, rec = self.state, self.policies, self.rec
        text = turn.text or ""
        if not text.strip():
            prompt = pol.recovery.on_empty(state)
            if prompt:
                state.empties += 1
                state.messages.append({"role": "user", "content": prompt})
                rec.emit("model.retry", {"why": "empty completion", "attempt": state.empties})
                self.steps.append(Step(state.step, turn, [], ControllerState.CONTINUE))
                return None
        verdict = pol.verify.accept_final(state, text)
        rec.emit("verify.result", {"command": "accept_final", "exit": 0 if verdict.ok else 1,
                                   "passed": int(verdict.ok), "failed": int(not verdict.ok)})
        if verdict.ok:
            state.structured = verdict.structured
            self.steps.append(Step(state.step, turn, [], ControllerState.STOP))
            return self.end("completed", text=text, capped=state.answering)
        state.extra["_refused_why"] = verdict.why
        if state.reasks < pol.verify.max_reasks:
            state.reasks += 1
            state.messages.append({"role": "user", "content": pol.verify.reask(verdict.why)})
            self.steps.append(Step(state.step, turn, [], ControllerState.CONTINUE))
            return None
        return self.end("failed", stopped="refused", error=verdict.why)

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
        return KernelResult(
            status="suspended" if state.status == "awaiting_approval" else state.status,
            run_id=state.run_id, text=state.final_text or "", structured=state.structured,
            findings=list(state.findings), pending=state.pending, usage=state.usage,
            steps=state.step, exhausted=state.exhausted, stopped=state.stopped, error=error,
            trace=list(state.trace), events=list(rec.events))


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
                                   "lineage_id": state.lineage_id, "attempt": state.attempt})
    if spec.persist:
        drive.store.save(state)
    return drive.drive()


def resume(spec: RunSpec, *, decision: Optional[dict] = None) -> KernelResult:
    """Continue the suspended run `spec.run_id` from its checkpoint.

    Waiting on a person: `decision` must approve the held call ({"approved": True,
    "decision_id": ...}, plus `args_sha256` when the approval bound the arguments). The held call
    then runs WITHOUT asking its gates again — the approval is the answer to them — its result
    replaces the deferral the model saw, and the loop goes on. Anything else is `denied` and the
    run stays suspended. Waiting on the budget: the loop simply goes on.

    The ToolSet, engine and policies are the caller's to rebuild (a checkpoint holds data, never
    live objects), as resume_agent_run rebuilds its adapter and tools today."""
    store = spec.store if spec.store is not None else DEFAULT_STORE
    state = store.load(spec.run_id or "")
    if state is None:
        return KernelResult(status="failed", run_id=spec.run_id or "", error="no such run")
    drive = _Run(spec, state)
    if state.status == "awaiting_approval":
        refusal = _approval_refusal(state, decision)
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
    return drive.drive()


def _approval_refusal(state: RunState, decision: Optional[dict]) -> str:
    pending = state.pending or {}
    if not decision or not decision.get("approved"):
        return "the held call was not approved"
    if str(decision.get("decision_id") or "") != str(pending.get("decision_id") or ""):
        return "that decision is not the one this run waits on"
    from ..services.argument_binding import held_call_matches

    if not held_call_matches(str(pending.get("tool") or ""), pending.get("args"),
                             decision.get("args_sha256")):
        return "arguments do not match the approved call"
    return ""


def _run_approved(drive: _Run, decision: dict) -> None:
    state, spec = drive.state, drive.spec
    pending = dict(state.pending or {})
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    call = ToolCall(id=str(pending.get("call_id") or ""), name=str(pending.get("tool") or ""),
                    arguments=dict(args), raw_arguments=json.dumps(args))
    key = state.idempotency_key(int(pending.get("step") or 0), call.id)
    if key in state.executed:
        result = result_from(call, {"status": "skipped", "note": "this exact call already ran"})
    else:
        result = drive.toolset.invoke(call, state=state, approved=decision)
        state.executed.append(key)
    body = drive.policies.context.render(result)
    for message in reversed(state.messages):
        if message.get("role") == "tool" and str(message.get("tool_call_id")) == call.id:
            message["content"] = body
            break
    state.rendered.append(body)
    state.trace.append({"step": int(pending.get("step") or 0), "tool": call.name, "args": args,
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
