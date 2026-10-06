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
        while True:
            try:
                if "model" in kwargs:
                    kwargs["model"] = self._model
                return getattr(self._agent, method)(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 — classified below, re-raised if not ours
                kind = note_failure(self._engine, error)
                if kind is None or hops >= MAX_HOPS:
                    raise
                hops += 1
                failed = self._engine
                agent, model, decision = self._reroute()
                if decision is None or getattr(decision, "refused", False) or \
                        getattr(decision, "engine", "") in ("", failed):
                    raise
                log.info("stage call failed on %s (%s); retrying on %s", failed, kind,
                         decision.engine)
                if self._on_hop is not None:
                    self._on_hop(failed, decision, kind)
                self._agent, self._model, self._engine = agent, model, decision.engine
                kwargs.pop("resume_session_id", None)   # a new source has no such session

    def run_role(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("run_role", *args, **kwargs)

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
