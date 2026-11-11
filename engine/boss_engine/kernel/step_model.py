"""One model call of a step, with every recovery the RecoveryPolicy allows.

What happens when a call fails used to depend on which loop made it: a 529 was retried only in
chat, a provider's 400 on a malformed call was fed back only in agent steps, the per-minute
budget was waited for only in the pre-check. Here the failure is classified once
(errors.classify) and the policy picks the move:

  retry     the same request again after backoff × attempt (not a step)
  wait      sleep the budget's retry-after and ask again (not a step), within the policy's bound
  feedback  tell the model its call was refused and go on (a step, as agent_loop counted it)
  answer    the turn's clock ran out mid-investigation: the next call is the answer (a step)
  suspend   park the run until the budget frees (budget_wait)
  fail      end the run, saying why
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Optional

from .engine import ModelRequest
from .errors import Cancelled, classify
from .types import ModelTurn


@dataclass
class Called:
    turn: Optional[ModelTurn] = None
    latency_ms: int = 0
    #: "" (a turn came back), "continue" (the step is spent; go round), "end" (stop the run).
    next: str = ""
    status: str = ""
    stopped: str = ""
    error: str = ""
    budget: Optional[dict] = None


def _deadline_scope(run: Any, answering: bool):
    """Under a turn budget no call outlives the turn (services/call_deadline.py): the adapters
    read the deadline this sets, as they do inside EngineAgent today."""
    budget = getattr(run.policies.stop, "turn_budget", None)
    if budget is None:
        return contextlib.nullcontext()
    from ..services.call_deadline import until

    return until(budget.deadline(answering=answering))


def _inline_pause(retry_after: Optional[float], left: float) -> float:
    try:
        from ..services.budget import burst_retry_after_seconds

        wait = float(burst_retry_after_seconds(retry_after))
    except Exception:  # noqa: BLE001 — no budget module: the deferral's own number, else 30 s
        wait = float(retry_after or 30)
    return max(0.0, min(wait, left))


def call_model(run: Any, req: ModelRequest, *, answering: bool) -> Called:
    state, rec, pol, spec = run.state, run.rec, run.policies, run.spec
    budget = getattr(pol.stop, "turn_budget", None)
    if budget is not None:
        budget.spend_call()
    attempts = 0
    while True:
        if run.cancelled():
            return Called(next="end", status="cancelled", stopped="cancelled")
        attempts += 1
        rec.emit("model.request", {"attempt": attempts, "tools": [t.name for t in req.tools],
                                   "answering": answering, "messages": len(req.messages)})
        started = spec.clock()
        try:
            with _deadline_scope(run, answering):
                turn = run.engine.complete(req, on_delta=spec.on_delta, cancel=spec.cancel)
        except Cancelled:
            return Called(next="end", status="cancelled", stopped="cancelled")
        except Exception as exc:  # noqa: BLE001 — sorted by what it is, below
            error = classify(exc)
            action = pol.recovery.on_model_error(state, error, attempts)
            rec.emit("model.error", {"error": error.detail, "class": error.kind,
                                     "action": action, "attempt": attempts},
                     latency_ms=int((spec.clock() - started) * 1000))
            if action == "retry":
                rec.emit("model.retry", {"attempt": attempts + 1, "class": error.kind})
                spec.sleep(pol.recovery.backoff_seconds * attempts)
                continue
            if action == "wait":
                pause = _inline_pause(error.retry_after_seconds,
                                      pol.recovery.max_wait_seconds - state.waited_seconds)
                rec.emit("budget.wait", {"mode": "inline", "seconds": pause,
                                         "detail": error.detail})
                began = spec.clock()
                spec.sleep(pause)
                state.waited_seconds += max(pause, spec.clock() - began)
                state.waits += 1
                continue
            if action == "feedback":
                state.refused_calls += 1
                state.step += 1
                state.messages.append({"role": "user",
                                       "content": pol.recovery.refused_feedback(error.detail)})
                rec.emit("schema.invalid", {"by": "provider", "detail": error.detail,
                                            "attempt": state.refused_calls})
                return Called(next="continue")
            if action == "answer":
                state.out_of_time = True
                state.step += 1
                return Called(next="continue")
            if action == "suspend":
                return Called(next="end", status="budget_wait", stopped="budget_wait",
                              error=error.detail,
                              budget={"retry_after_seconds": error.retry_after_seconds,
                                      "refusal": error.detail, "model": True})
            if error.kind == "deadline":
                # Cut off while ANSWERING: the run is over and nothing is wrong with it. It ends
                # complete with no text; the caller writes its report from the trace, as
                # EngineAgent's out-of-time report does.
                return Called(next="end", status="completed", stopped="deadline",
                              error=error.detail)
            stopped = "budget_wait_limit" if error.kind == "budget_wait" else error.kind
            return Called(next="end", status="failed", stopped=stopped, error=error.detail)
        return Called(turn=turn, latency_ms=int((spec.clock() - started) * 1000))


__all__ = ["Called", "call_model"]
