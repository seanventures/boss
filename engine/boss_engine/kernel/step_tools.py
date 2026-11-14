"""The tool calls of one step: checked, run (or held), answered, recorded.

In order, for each call the model made:

  1. a stop asked for? the call is answered "skipped" and not run;
  2. an earlier call of this step is waiting (a person or the budget)? "skipped", as agent_loop
     answered them ("a prior action this turn awaits approval");
  3. otherwise the call goes down the wrap_tool_call chain (kernel/middleware.py): the spec's
     middleware, then `tool_args` (not a tool offered this step? refused, naming that; arguments
     that break the tool's schema? refused with the JSON pointer of the fault, and the model may
     write the call again — `RecoveryPolicy.invalid_repairs` — then the call fails), then the
     mandatory governance: `authz` (the run token decides the permission; a hold must be a
     Decision), `tool_screen` (an outside result screened and remembered), `audit` (a write whose
     idempotency key — run:step:call — already ran is "skipped": resuming twice never runs a
     write twice), and the ToolSet runs it (its own gates — holds, deferrals — decide inside).

Every call gets a tool message, so the transcript a provider is sent is never malformed. After
the step: the same-tool nudge (RecoveryPolicy.on_tool_error), the workspace snapshot.

DURABLE (AP-P2, a checkpointer with pending writes — kernel/durable.py): a write tool's call is
marked `started` before it runs and every result is written as it lands. A batch finished after
a crash (`recorded`: the writes of the in-flight checkpoint) replays the calls that finished,
re-runs the ones that never started, and parks an irreversible call that started without a
result on an unknown-outcome interrupt (kernel/idempotency.py). With `parallel_interrupts`
every held call of the step waits together, each on its own interrupt id.
"""
from __future__ import annotations

from typing import Any, Optional

from . import idempotency
from .interrupts import APPROVAL, QUESTION, UNKNOWN_OUTCOME, Interrupt, call_interrupt_id
from .middleware import ToolRequest
from .types import ToolCall, ToolResult

AWAITING = "a prior action this turn awaits approval"
BUDGET_BUSY = "BOSS's per-minute budget is busy; try again shortly."
CANCELLED = "the run was stopped before this call ran"


def _skipped(call: ToolCall, note: str) -> ToolResult:
    return ToolResult(call.id, call.name, "skipped", {"status": "skipped", "note": note})


def invalid(call: ToolCall, why: str, *, fail: bool) -> ToolResult:
    if fail:
        message = (f"'{call.name}' was NOT run: its arguments broke the tool's parameters again "
                   f"({why}). Do not call it that way again; use what you have, or another tool.")
    else:
        message = (f"'{call.name}' was NOT run, so nothing changed: its arguments do not match "
                   f"the tool's parameters at {why}. Read the parameters again and make the call "
                   "again with every required property, each of the declared type.")
    return ToolResult(call.id, call.name, "refused",
                      {"ok": False, "status": "refused", "error": "invalid_arguments",
                       "where": why, "message": message})


def _store(run: Any) -> Any:
    store = getattr(run, "store", None)
    durable = getattr(run, "durable", None)
    return store if callable(durable) and durable() else None


def _guarded(schema: Any) -> bool:
    """A call whose start is recorded before it runs: anything that is not a plain read."""
    return schema is not None and (schema.kind == "write"
                                   or getattr(schema, "idempotency", None) is not None)


def _from_record(call: ToolCall, record: dict) -> ToolResult:
    """A tool result written before the process stopped, as the ToolResult it was."""
    saved = record.get("result") if isinstance(record.get("result"), dict) else {}
    return ToolResult(call.id, call.name, str(record.get("status") or "ok"),
                      saved.get("content"), untrusted=bool(saved.get("untrusted")),
                      decision_id=saved.get("decision_id"), budget=saved.get("budget"),
                      pending=saved.get("pending"))


def _unknown_outcome(run: Any, call: ToolCall, step: int) -> ToolResult:
    """An irreversible call that started and left no result: it may have happened. It is NOT
    run again; the run waits on an unknown-outcome interrupt (a Decision when the run has an
    interrupt port) for a person to say what happened."""
    state = run.state
    item = Interrupt(id=call_interrupt_id(state.run_id, step, call.id), kind=UNKNOWN_OUTCOME,
                     payload={"summary": f"'{call.name}' may or may not have run before BOSS "
                                         "stopped. Say whether it happened; it is never run "
                                         "twice on a guess."},
                     schema={"fields": [{"key": "happened", "type": "checkbox",
                                         "label": "It happened"}]},
                     tool=call.name, args=call.arguments if isinstance(call.arguments, dict)
                     else {}, call_id=call.id, step=step)
    port = getattr(run.spec, "interrupts", None)
    decision_id = port.open(state, item) if port is not None else None
    item.decision_id = decision_id
    run.rec.emit("escalation", {"tool": call.name, "why": "unknown_outcome",
                                "interrupt_id": item.id, "decision_id": decision_id})
    return ToolResult(call.id, call.name, "deferred",
                      {"status": "unknown_outcome", "interrupt_id": item.id,
                       "decision_id": decision_id,
                       "note": "this call may have happened before BOSS stopped; a person is "
                               "asked what happened before anything else is done with it"},
                      decision_id=decision_id, pending={"interrupt": item.as_pending()})


def run_calls(run: Any, calls: list, offered: list, *,
              recorded: Optional[dict] = None) -> Optional[dict]:
    """Run one step's calls. Returns None to continue, or {"status", "pending"} to end the run
    (suspended for a person, waiting for the budget, or cancelled). `recorded` is a crashed
    batch's pending writes, by call id (see the module doc)."""
    state, rec, pol, spec = run.state, run.rec, run.policies, run.spec
    by_name = {t.name: t for t in offered}
    step = state.step
    store = _store(run)
    parallel = bool(getattr(spec, "parallel_interrupts", False))
    held: Optional[ToolResult] = None
    holds: list = []
    budget: Optional[ToolResult] = None
    cancelled = False
    parked = False
    nudges: list = []
    wrote = False
    for idx, call in enumerate(calls):
        schema = by_name.get(call.name)
        record = (recorded or {}).get(call.id)
        rec.emit("tool.requested", {"tool": call.name, "call_id": call.id,
                                    "args": call.arguments if call.arguments is not None
                                    else call.raw_arguments})
        if not cancelled and run.cancelled():
            cancelled = True
        keep = True  # record this result as the call's pending write
        if record is not None and record.get("status") != "started":
            # FINISHED BEFORE THE PROCESS STOPPED: its result is replayed, never re-run.
            result = _from_record(call, record)
            keep = False
            if schema is not None and schema.kind == "write" and result.status == "ok":
                key = state.idempotency_key(step, call.id)
                if key not in state.executed:
                    state.executed.append(key)
        elif cancelled:
            result, keep = _skipped(call, CANCELLED), False
        elif held is not None and not parallel:
            result, keep = _skipped(call, AWAITING), False
        elif budget is not None:
            result, keep = _skipped(call, BUDGET_BUSY), False
        elif record is not None and not idempotency.may_rerun(schema, started=True):
            result = _unknown_outcome(run, call, step)
            parked = True
        else:
            if store is not None and _guarded(schema):
                store.start_write(state, call=call, idx=idx,
                                  idempotency=idempotency.class_of(schema),
                                  key=state.idempotency_key(step, call.id))
            request = ToolRequest(call=call, schema=schema, step=step)
            result = run.chain.tool_call(run, request, run.invoke_tool)
            wrote = wrote or bool(request.notes.get("wrote"))
        _answer(run, call, result, step, write=bool(schema and schema.kind == "write"),
                idx=idx, store=store if keep else None, schema=schema)
        if result.status == "deferred":
            if result.budget is not None:
                budget = result
            else:
                holds.append(result)
                if held is None:
                    held = result
        if result.status in ("error", "refused") and not (cancelled or held or budget):
            failures = int(state.tool_failures.get(call.name) or 0) + 1
            state.tool_failures[call.name] = failures
            nudge = pol.recovery.on_tool_error(state, result, failures)
            if nudge:
                nudges.append(nudge)
                finding = pol.recovery.finding_for(call.name, failures)
                state.findings.append(finding)
                rec.emit("finding", {"source": finding.source, "severity": finding.severity,
                                     "message": finding.message})
    for nudge in nudges:
        state.messages.append({"role": "user", "content": nudge})
    if wrote and spec.workspace is not None:
        from .workspace import tree_sha

        ref = tree_sha(spec.workspace)
        if ref:
            state.workspace_ref = ref
            rec.emit("workspace.snapshot", {"tree": ref, "step": step}, workspace_ref=ref)
    if cancelled:
        return {"status": "cancelled", "pending": None}
    if budget is not None:
        return {"status": "budget_wait",
                "pending": {"kind": "budget_wait", "budget": budget.budget or {},
                            "tool": budget.name, "call_id": budget.call_id, "step": step}}
    if held is not None:
        args = next((c.arguments for c in calls if c.id == held.call_id), None) or {}
        items = [_interrupt_item(state, step, r, calls) for r in holds]
        pending = {**(held.pending or {}), "tool": held.name, "args": args,
                   "call_id": held.call_id, "decision_id": held.decision_id, "step": step,
                   "interrupt_id": items[0]["id"], "interrupts": items}
        pending.pop("interrupt", None)
        if parked:
            pending["parked"] = UNKNOWN_OUTCOME
        return {"status": "awaiting_approval", "pending": pending,
                "stopped": UNKNOWN_OUTCOME if parked else "awaiting_approval"}
    return None


def _interrupt_item(state: Any, step: int, result: ToolResult, calls: list) -> dict:
    """One held call as the interrupt a resume answers by id."""
    pending = dict(result.pending or {})
    declared = dict(pending.get("interrupt") or {})
    args = next((c.arguments for c in calls if c.id == result.call_id), None) or {}
    binding = declared.get("binding")
    if pending.get("args_sha256"):
        binding = {**(binding or {}), "args_sha256": pending["args_sha256"]}
    return {"id": declared.get("id") or pending.get("interrupt_id")
            or call_interrupt_id(state.run_id, step, result.call_id),
            "kind": declared.get("kind") or (QUESTION if pending.get("question") else APPROVAL),
            "decision_id": result.decision_id, "tool": result.name, "args": args,
            "call_id": result.call_id, "step": step, "binding": binding,
            "schema": declared.get("schema"), "payload": declared.get("payload")}


def _answer(run: Any, call: ToolCall, result: ToolResult, step: int, *, write: bool,
            idx: int = 0, store: Any = None, schema: Any = None) -> None:
    """The tool message the model reads, the trace entry, the ledger event — and, on a durable
    run, the call's pending write."""
    state, rec = run.state, run.rec
    body = run.policies.context.render(result)
    if store is not None:
        store.put_write(state, call=call, idx=idx, status=result.status,
                        result={"content": result.content, "untrusted": result.untrusted,
                                "decision_id": result.decision_id, "budget": result.budget,
                                "pending": result.pending},
                        rendered=body, idempotency=idempotency.class_of(schema)
                        if schema is not None else None,
                        key=state.idempotency_key(step, call.id))
    state.rendered.append(body)
    state.messages.append({"role": "tool", "tool_call_id": call.id, "content": body})
    state.trace.append({"step": step, "tool": call.name,
                        "args": call.arguments if call.arguments is not None else {},
                        "status": result.status, "result": result.content,
                        "call_id": call.id})
    kind = {"refused": "tool.refused", "deferred": "tool.deferred"}.get(result.status,
                                                                         "tool.result")
    data = {"tool": call.name, "call_id": call.id, "status": result.status,
            "args": call.arguments if isinstance(call.arguments, dict) else {},
            "decision_id": result.decision_id}
    if write:
        data["idempotency_key"] = state.idempotency_key(step, call.id)
    rec.emit(kind, data, latency_ms=int(state.extra.pop("_last_tool_latency", 0) or 0))


__all__ = ["AWAITING", "BUDGET_BUSY", "CANCELLED", "invalid", "run_calls"]
