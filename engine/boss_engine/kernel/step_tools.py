"""The tool calls of one step: checked, run (or held), answered, recorded.

In order, for each call the model made:

  1. a stop asked for? the call is answered "skipped" and not run;
  2. an earlier call of this step is waiting (a person or the budget)? "skipped", as agent_loop
     answered them ("a prior action this turn awaits approval");
  3. not a tool offered this step? refused, naming that;
  4. arguments that break the tool's schema? refused with the JSON pointer of the fault, and the
     model may write the call again (`RecoveryPolicy.invalid_repairs`), then the call fails;
  5. a write whose idempotency key (run:step:call) already ran? "skipped" — resuming twice never
     runs a write twice;
  6. otherwise the ToolSet runs it (its gates — identity, holds, deferrals — decide inside).

Every call gets a tool message, so the transcript a provider is sent is never malformed. After
the step: the same-tool nudge, the workspace snapshot, the VerifyPolicy's findings.
"""
from __future__ import annotations

from typing import Any, Optional

from .schema_check import check_arguments
from .toolset import unknown
from .types import ToolCall, ToolResult

AWAITING = "a prior action this turn awaits approval"
BUDGET_BUSY = "BOSS's per-minute budget is busy; try again shortly."
CANCELLED = "the run was stopped before this call ran"


def _skipped(call: ToolCall, note: str) -> ToolResult:
    return ToolResult(call.id, call.name, "skipped", {"status": "skipped", "note": note})


def _invalid(call: ToolCall, why: str, *, fail: bool) -> ToolResult:
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


def run_calls(run: Any, calls: list, offered: list) -> Optional[dict]:
    """Run one step's calls. Returns None to continue, or {"status", "pending"} to end the run
    (suspended for a person, waiting for the budget, or cancelled)."""
    state, rec, pol, spec = run.state, run.rec, run.policies, run.spec
    by_name = {t.name: t for t in offered}
    step = state.step
    held: Optional[ToolResult] = None
    budget: Optional[ToolResult] = None
    cancelled = False
    nudges: list = []
    wrote = False
    for call in calls:
        schema = by_name.get(call.name)
        rec.emit("tool.requested", {"tool": call.name, "call_id": call.id,
                                    "args": call.arguments if call.arguments is not None
                                    else call.raw_arguments})
        if not cancelled and run.cancelled():
            cancelled = True
        if cancelled:
            result = _skipped(call, CANCELLED)
        elif held is not None:
            result = _skipped(call, AWAITING)
        elif budget is not None:
            result = _skipped(call, BUDGET_BUSY)
        elif schema is None:
            result = unknown(call)
        else:
            why = check_arguments(call.arguments, schema.parameters)
            if why:
                state.invalid[call.name] = int(state.invalid.get(call.name) or 0) + 1
                verdict = pol.recovery.on_invalid(state, why, state.invalid[call.name])
                rec.emit("schema.invalid", {"by": "kernel", "tool": call.name, "where": why,
                                            "attempt": state.invalid[call.name],
                                            "verdict": verdict})
                result = _invalid(call, why, fail=verdict == "fail")
            else:
                if state.invalid.get(call.name):
                    rec.emit("schema.repaired", {"tool": call.name})
                    state.invalid[call.name] = 0
                key = state.idempotency_key(step, call.id)
                if schema.kind == "write" and key in state.executed:
                    result = _skipped(call, "this exact call already ran")
                else:
                    started = spec.clock()
                    result = run.toolset.invoke(call, state=state)
                    if schema.kind == "write" and result.status == "ok":
                        state.executed.append(key)
                        wrote = True
                    state.extra["_last_tool_latency"] = int((spec.clock() - started) * 1000)
        _answer(run, call, result, step, write=bool(schema and schema.kind == "write"))
        if result.status == "deferred":
            if result.budget is not None:
                budget = result
            else:
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
        return {"status": "awaiting_approval",
                "pending": {**(held.pending or {}), "tool": held.name, "args": args,
                            "call_id": held.call_id, "decision_id": held.decision_id,
                            "step": step}}
    return None


def _answer(run: Any, call: ToolCall, result: ToolResult, step: int, *, write: bool) -> None:
    """The tool message the model reads, the trace entry, and the ledger event."""
    state, rec = run.state, run.rec
    body = run.policies.context.render(result)
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


__all__ = ["AWAITING", "BUDGET_BUSY", "CANCELLED", "run_calls"]
