"""What a failed model call IS, decided once for every loop.

Each loop sorted exceptions its own way: EngineAgent retried a 529 and nothing else did;
agent_loop fed a provider's 400 on a malformed tool call back to the model and nothing else did;
the pre-check waited out the per-minute budget and the decision loop gave up on it. The kernel
classifies once, and the RecoveryPolicy decides what each class gets.

The classes:

  transient     a provider hiccup (529, 503, reset, timeout): the same call again, shortly
  refused_call  the provider refused a malformed tool call before it ran (Groq's 400
                "tool_use_failed"): the model is told and writes the call again
  budget_wait   the per-minute budget deferred the call (ModelSpendDeferred): it frees itself
  budget_stop   the budget refused the call (ModelSpendRefused): a person must lift the cap
  deadline      the turn's clock ran out (TurnDeadlineExceeded): answer from what is in hand
  cancelled     a stop was asked for
  fatal         anything else: the run fails and says why

The services are imported inside `classify`, never at module import: the kernel is a leaf
package (design §2.1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

ErrorKind = Literal["transient", "refused_call", "budget_wait", "budget_stop", "deadline",
                    "cancelled", "fatal"]


class Cancelled(RuntimeError):
    """Raised inside the kernel when the run's CancelToken is set mid-call."""


@dataclass(frozen=True)
class Classified:
    kind: ErrorKind
    detail: str
    retry_after_seconds: Optional[float] = None


def provider_refused_the_call(error: BaseException) -> Optional[str]:
    """The provider's own words when `error` is it refusing a malformed tool call, else None.

    The two cases arrive identically, as a 400 from the completion request. A provider that
    validates tool calls server-side (Groq does) refuses the malformed one and never returns a
    message. Recognised, it is one turn of feedback; anything else stays an exception, because a
    real outage must not be retried as though the agent could word its way out of it."""
    detail = str(error)
    lowered = detail.lower()
    if "tool_use_failed" in lowered or "tool call validation failed" in lowered:
        return detail[:600]
    return None


def classify(error: BaseException) -> Classified:
    """Sort `error` into one class. Order matters: a budget deferral's text can contain words
    the transient check would match, and the turn's deadline must never read as a hiccup."""
    if isinstance(error, Cancelled):
        return Classified("cancelled", str(error) or "cancelled")
    text = f"{type(error).__name__}: {error}"
    try:
        from ..services.call_deadline import TurnDeadlineExceeded

        if isinstance(error, TurnDeadlineExceeded):
            return Classified("deadline", text[:600])
    except ImportError:  # pragma: no cover — the services package is always present in BOSS
        pass
    try:
        from ..services.model_routing import ModelSpendDeferred, ModelSpendRefused

        if isinstance(error, ModelSpendDeferred):
            return Classified("budget_wait", text[:600],
                              getattr(error, "retry_after_seconds", None))
        if isinstance(error, ModelSpendRefused):
            return Classified("budget_stop", text[:600])
    except ImportError:  # pragma: no cover
        pass
    refused = provider_refused_the_call(error)
    if refused is not None:
        return Classified("refused_call", refused)
    from ..agent.transient import _looks_transient

    if _looks_transient(error):
        return Classified("transient", text[:600])
    return Classified("fatal", text[:600])


__all__ = ["Cancelled", "Classified", "ErrorKind", "classify", "provider_refused_the_call"]
