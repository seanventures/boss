"""Never stranded, at call time (Decision 14).

A route is chosen at a boundary, but a source can fail between the choice and the call: a 503,
a timeout, a weekly limit reached mid-build. A failed call has no session to preserve, so the
SAME step retries at once on the next source in its tier:

  * the failure is classified (model_router.breaker.kind_of): a limit or a transient failure —
    anything else (a bad prompt, a crash) is the work's own problem and is raised unchanged;
  * the failing source is tripped in the circuit breaker (and a limit is recorded on its account
    by telemetry, so every project learns it);
  * the step is routed again — the router now skips that source — and the call is repeated, at
    most MAX_HOPS times.

Two wrappers, one rule: `FailoverAgent` for build stages (a CodeAgent's `run_role`/`run_research`),
`FailoverChatAdapter` for chat, agent, panel and skill calls. Smart mode only: Fixed mode keeps
its own behaviour (spend-cap failover, the legacy errors).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from . import breaker

log = logging.getLogger("boss.model_router.failover")

MAX_HOPS = 2


def _refusal_base():
    from ..github_adapter import IntegrationConfigurationError

    return IntegrationConfigurationError


class RouteRefused(_refusal_base()):  # type: ignore[misc]
    """No source may take this work under the policy (Local means local; Frontier never goes
    local; no metered spend while the budget is spent). Settled: the message says what fixes it."""


def _seconds_until_reset(error: str) -> Optional[float]:
    from datetime import datetime, timezone

    from .headroom import windows_from_limit_message

    windows = windows_from_limit_message(error)
    if not windows:
        return None
    soonest = min((w.get("resets_at") for w in windows.values() if w.get("resets_at")),
                  default=None)
    if not soonest:
        return None
    delta = (datetime.fromisoformat(soonest) - datetime.now(timezone.utc)).total_seconds()
    # The breaker covers the next minute or two; the ACCOUNT's windows carry the long reset
    # (and are what every project reads), so the breaker never outlives what it stands for.
    return max(30.0, min(delta, 120.0))


def note_failure(engine: str, error: BaseException | str) -> Optional[str]:
    """Trip the breaker for a call-time failure. Returns its kind, or None (not ours)."""
    text = str(error)
    kind = breaker.kind_of(text)
    if kind is None:
        return None
    breaker.trip(engine, text.strip().splitlines()[0][:120] if text.strip() else kind,
                 _seconds_until_reset(text) if kind == "limit" else None)
    return kind


#: How long a stage waits for a temporarily failing source before it fails (Decision 21), and
#: the backoff between asks — each ask is a half-open trial of the source (breaker.claim_trial).
WAIT_BOUND_S = 600.0
BACKOFF_S = (5.0, 10.0, 20.0, 30.0, 60.0)


def wait_for_route(route: Callable[[], Any], *,
                   on_wait: Optional[Callable[[Any, float, float], None]] = None,
                   bound_s: Optional[float] = None,
                   sleep: Callable[[float], None] = time.sleep) -> Any:
    """NEVER STRANDED BY A TEMPORARY OUTAGE: a route refused only because its sources are out
    for now (`retry_after_s`) is asked again after a bounded backoff, with the reason in words
    (`on_wait(decision, pause_s, waited_s)`), until it routes or the bound passes. A refusal
    that is not temporary (Local means local, the budget is spent) returns at once."""
    from dataclasses import is_dataclass, replace

    bound = WAIT_BOUND_S if bound_s is None else float(bound_s)
    decision = route()
    waited, n = 0.0, 0
    while getattr(decision, "refused", False) \
            and getattr(decision, "retry_after_s", None) is not None and waited < bound:
        pause = min(max(float(decision.retry_after_s), BACKOFF_S[min(n, len(BACKOFF_S) - 1)]),
                    bound - waited)
        if on_wait is not None:
            try:
                on_wait(decision, pause, waited)
            except Exception:  # noqa: BLE001 — the record of a wait is not the wait
                log.warning("could not record a wait", exc_info=True)
        sleep(pause)
        waited += pause
        n += 1
        decision = route()
    if getattr(decision, "refused", False) and waited and is_dataclass(decision):
        decision = replace(decision, refusal=f"{decision.refusal} Waited {_minutes(waited)} "
                                             "for it to recover.", retry_after_s=None)
    return decision


def waiting_words(decision: Any, pause: float, step: str = "") -> str:
    """'Write tests is waiting for a model: … Asking again in 20 s.'"""
    from .tiers import step_label

    what = step_label(step) if step else "This work"
    return (f"{what} is waiting for a model: {decision.refusal.rstrip('.')}. Asking again in "
            f"{int(round(pause))} s (gives up after {_minutes(WAIT_BOUND_S)}).")


def _minutes(seconds: float) -> str:
    return f"{int(round(seconds))} s" if seconds < 90 else f"{round(seconds / 60)} min"


class FailoverAgent:
    """A build stage's agent that, on a limit or transient failure, retries the same call on
    the next source the router picks. `reroute()` → (agent, model, decision)."""

    def __init__(self, agent: Any, model: Optional[str], engine: str,
                 reroute: Callable[[], tuple[Any, Optional[str], Any]],
                 on_hop: Optional[Callable[[str, Any, str], None]] = None):
        self._agent, self._model, self._engine = agent, model, engine
        self._reroute, self._on_hop = reroute, on_hop

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        hops = 0
        trials_since: Optional[float] = None
        while True:
            try:
                if "model" in kwargs:
                    kwargs["model"] = self._model
                out = getattr(self._agent, method)(*args, **kwargs)
                breaker.close(self._engine)          # it ran: not failing (a half-open trial)
                return out
            except Exception as error:  # noqa: BLE001 — classified below, re-raised if not ours
                kind = note_failure(self._engine, error)
                if kind is None or hops >= MAX_HOPS:
                    raise
                failed = self._engine
                agent, model, decision = self._reroute()
                trial = "half_open" in tuple(getattr(decision, "rules", ()) or ())
                if decision is None or getattr(decision, "refused", False) or \
                        getattr(decision, "engine", "") == "" or (
                        decision.engine == failed and not trial):
                    raise
                # A half-open trial (after a bounded wait) does not spend a hop: the wait's
                # bound is what ends it — WAIT_BOUND_S from the first trial, then the error.
                if trial:
                    trials_since = trials_since or time.monotonic()
                    if time.monotonic() - trials_since > WAIT_BOUND_S:
                        raise
                else:
                    hops += 1
                log.info("stage call failed on %s (%s); retrying on %s", failed, kind,
                         decision.engine)
                if self._on_hop is not None:
                    self._on_hop(failed, decision, kind)
                self._agent, self._model, self._engine = agent, model, decision.engine
                kwargs.pop("resume_session_id", None)   # a new source has no such session
                from ..engine_contract import capabilities_of

                if not capabilities_of(agent).budget_limit:
                    # A spend ceiling only an engine that stops itself takes; the caller's
                    # stage boundary still enforces it on this one.
                    kwargs.pop("max_budget_usd", None)

    def run_role(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("run_role", *args, **kwargs)

    def run_plain(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("run_plain", *args, **kwargs)

    def run_research(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("run_research", *args, **kwargs)


class FailoverChatAdapter:
    """A chat adapter that records each call against its route (llm_calls.route_id) and, on a
    limit or transient failure, retries the call once more on the next source."""

    def __init__(self, inner: Any, decision: Any,
                 reroute: Callable[[str], tuple[Any, Any]], *, stage: str = "chat",
                 record: bool = True):
        self._inner, self._decision = inner, decision
        self._reroute, self._stage, self._record = reroute, stage, record

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def route(self) -> Any:
        return self._decision

    def _record_call(self, usage: Any, started: float, ok: bool, error: Optional[str]) -> None:
        if not self._record:
            return
        try:
            from ..llm_telemetry import record_llm_call
            from ..openai_adapter import estimate_cost_usd_micros

            usage = usage if isinstance(usage, dict) else {}
            record_llm_call(engine_name=self._decision.engine, usage=usage,
                            cost_usd_micros=estimate_cost_usd_micros(usage) if usage else 0,
                            latency_ms=int((time.monotonic() - started) * 1000), ok=ok,
                            error=error, stage=self._stage, route_id=self._decision.id)
        except Exception:  # noqa: BLE001
            log.warning("could not record a routed call", exc_info=True)

    def _call(self, method: str, usage_at: int, *args: Any, **kwargs: Any) -> Any:
        hops = 0
        while True:
            started = time.monotonic()
            try:
                result = getattr(self._inner, method)(*args, **kwargs)
                breaker.close(self._decision.engine)
            except Exception as error:  # noqa: BLE001
                self._record_call({"model": self._decision.model or ""}, started, False,
                                  str(error))
                kind = note_failure(self._decision.engine, error)
                if kind is None or hops >= MAX_HOPS:
                    raise
                hops += 1
                inner, decision = self._reroute(self._decision.engine)
                if decision is None or getattr(decision, "refused", False) or \
                        decision.engine in ("", self._decision.engine):
                    raise
                self._inner, self._decision = inner, decision
                continue
            usage = result[usage_at] if isinstance(result, tuple) and len(result) > usage_at \
                else {}
            self._record_call(usage, started, True, None)
            return result

    def create_chat_completion(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("create_chat_completion", 1, *args, **kwargs)

    def create_text_response(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("create_text_response", 2, *args, **kwargs)
