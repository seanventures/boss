"""A model call that never got a slot has to leave a trace.

This is the defect behind "Engineer implementing — 27m46s" with nothing in any log. Every
failure INSIDE the invocation already recorded an `llm_call` with ok=False. But the limiter's
`slot()` blocks and can time out BEFORE the invocation is ever called, and that path recorded
nothing at all: no call, no turns, and not even the pacing line — which is emitted inside the
`with` block that was never entered.

The pipeline then classifies "could not acquire a …" as a check that did not COMPLETE and
re-runs the stage, so the cost multiplies while the evidence stays at zero. Across the run
history that shows up as long phases landing on multiples of the 600s acquire deadline
(~10m, ~20m, ~30m) with no model call inside them.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from boss_engine.services.code_agent import PacedCodeAgent, _SlotWatchdog
from boss_engine.services.model_rate_limiter import (
    ModelRateLimiter,
    RateLimitAcquireTimeout,
)


class _Inner:
    engine = "claude_code"

    def __init__(self):
        self.settings = type("S", (), {"claude_cli_model": "claude-sonnet-5"})()
        self.calls = 0

    def run_role(self, *a, **k):
        self.calls += 1
        return {"ok": True}


class _NoSlotLimiter:
    """A limiter whose lane never frees up, exactly as `_acquire` behaves at its deadline."""

    def __init__(self, exc=None):
        self._exc = exc or RateLimitAcquireTimeout(
            "could not acquire a anthropic call slot within 600s "
            "(in_flight=1/1, backoff_remaining=42s)."
        )

    @contextmanager
    def slot(self, provider):  # never yields
        raise self._exc
        yield  # pragma: no cover

    def lane_state(self, provider):
        return {"in_flight": 1, "limit": 1,
                "backoff_remaining_seconds": 42.0, "pacing_wait_seconds": 0.0}

    def report_ok(self, provider): ...
    def report_rate_limited(self, provider, retry_after_s=None): ...


def _paced(limiter):
    return PacedCodeAgent(_Inner(), limiter)


def test_a_call_that_never_got_a_slot_is_recorded(monkeypatch):
    """The whole point: the ledger must show the wait, not swallow it."""
    recorded: list[dict] = []
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call",
                        lambda **kw: recorded.append(kw))

    agent = _paced(_NoSlotLimiter())
    with pytest.raises(RateLimitAcquireTimeout):
        agent.run_role("prompt", "/tmp")

    assert len(recorded) == 1, "a dispatch that never started left no trace"
    assert recorded[0]["ok"] is False
    assert "never dispatched" in recorded[0]["error"]


def test_the_recorded_wait_is_the_time_actually_spent_waiting(monkeypatch):
    """`latency_ms` of zero would read as 'instant failure' — the opposite of the problem."""
    recorded: list[dict] = []
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call",
                        lambda **kw: recorded.append(kw))

    ticks = iter([100.0, 700.0, 700.0, 700.0])
    monkeypatch.setattr("boss_engine.services.code_agent.time.monotonic",
                        lambda: next(ticks, 700.0))

    agent = _paced(_NoSlotLimiter())
    with pytest.raises(RateLimitAcquireTimeout):
        agent.run_role("prompt", "/tmp")

    assert recorded[0]["latency_ms"] == 600_000


def test_the_original_exception_reaches_the_caller_unchanged(monkeypatch):
    """Recording is instrumentation. It must not become error handling: the pipeline's
    inconclusive-check classifier matches on the engine's own wording, so swallowing or
    rewrapping the exception would make a non-completion look like a verdict about the code."""
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call", lambda **kw: None)
    original = RateLimitAcquireTimeout("could not acquire a anthropic call slot within 600s")

    agent = _paced(_NoSlotLimiter(original))
    with pytest.raises(RateLimitAcquireTimeout) as caught:
        agent.run_role("prompt", "/tmp")
    assert caught.value is original


def test_a_failure_after_dispatch_is_not_reported_as_never_dispatched(monkeypatch):
    """`acquired_at` is the discriminator. If it were wrong, every ordinary agent error would
    be relabelled as a lane problem and the two would stop being distinguishable — which is
    the exact confusion this change exists to remove."""
    recorded: list[dict] = []
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call",
                        lambda **kw: recorded.append(kw))

    class _Boom(_Inner):
        def run_role(self, *a, **k):
            raise RuntimeError("the agent itself failed")

    agent = PacedCodeAgent(_Boom(), ModelRateLimiter(max_concurrency=1, min_interval_s=0))
    with pytest.raises(RuntimeError, match="the agent itself failed"):
        agent.run_role("prompt", "/tmp")

    assert recorded == [], "an in-flight failure was misreported as never dispatched"


def test_a_diagnostic_failure_never_breaks_the_dispatch(monkeypatch):
    """If recording the wait could itself raise, the observability change would become a new
    source of failed runs."""
    def _explode(**kw):
        raise RuntimeError("telemetry is down")

    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call", _explode)
    agent = _paced(_NoSlotLimiter())
    with pytest.raises(RateLimitAcquireTimeout):  # the ORIGINAL error, not the telemetry one
        agent.run_role("prompt", "/tmp")


def test_a_successful_call_still_dispatches_and_records_nothing_extra(monkeypatch):
    """The happy path must be untouched."""
    recorded: list[dict] = []
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call",
                        lambda **kw: recorded.append(kw))

    inner = _Inner()
    agent = PacedCodeAgent(inner, ModelRateLimiter(max_concurrency=1, min_interval_s=0))
    assert agent.run_role("prompt", "/tmp") == {"ok": True}
    assert inner.calls == 1
    assert recorded == []


# ── The lane's own numbers ─────────────────────────────────────────────────────

def test_the_lane_reports_why_there_is_no_slot():
    """"No slot" has three different causes with three different fixes: the lane is full, it
    is backing off after a 429, or it is pacing. A wait that does not say which is a wait
    nobody can act on."""
    limiter = ModelRateLimiter(max_concurrency=1, min_interval_s=0)
    state = limiter.lane_state("anthropic")
    assert set(state) == {"in_flight", "limit",
                          "backoff_remaining_seconds", "pacing_wait_seconds"}
    assert state["in_flight"] == 0

    limiter.report_rate_limited("anthropic", retry_after_s=30)
    assert limiter.lane_state("anthropic")["backoff_remaining_seconds"] > 0


def test_the_watchdog_speaks_while_the_wait_is_happening(caplog):
    """A post-mortem explains a stall after it has cost the time. WARNING, so the Activity Log
    keeps it without relying on an allowlist."""
    import logging

    watchdog = _SlotWatchdog(_NoSlotLimiter(), "anthropic")
    watchdog.INTERVAL_S = 0.01
    with caplog.at_level(logging.WARNING):
        watchdog.start()
        import time as _t
        _t.sleep(0.05)
        watchdog.stop()

    said = [r.getMessage() for r in caplog.records]
    assert any("still waiting for a anthropic model slot" in m for m in said), \
        "a long wait said nothing while it was happening"
    # The lane's numbers travel WITH the warning: "waiting" alone is not actionable.
    assert any("in_flight=1/1" in m and "backoff=42.0s" in m for m in said)


def test_the_watchdog_stops_and_does_not_outlive_the_dispatch():
    watchdog = _SlotWatchdog(_NoSlotLimiter(), "anthropic")
    watchdog.INTERVAL_S = 0.01
    watchdog.start()
    watchdog.stop()
    thread = watchdog._thread
    if thread is not None:
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert thread.daemon, "a watchdog thread must never hold the process open"
