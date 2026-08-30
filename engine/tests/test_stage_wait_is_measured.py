"""Waiting for a model lane is not the same as doing the work.

Measured across 70 real runs on goldeneye: 2,227 minutes of wall time against 652 minutes of
model latency — 71% of it was something other than the model thinking. The phase timings could
not say what: a stage that queued four minutes for a slot and then ran for one is recorded
identically to a stage that worked for five, and those two have opposite fixes.

`_guest_stage` dispatches every guest build inside `limiter.slot(provider)`, which BLOCKS on
concurrency, pacing and AIMD backoff. That block was invisible. Now it is a number.
"""

import inspect

from boss_engine.services.model_rate_limiter import (
    ModelRateLimiter, NullRateLimiter, SlotHandle,
)


def test_the_slot_reports_how_long_it_blocked():
    limiter = ModelRateLimiter(max_concurrency=2, min_interval_s=0.0)
    with limiter.slot("anthropic") as handle:
        assert isinstance(handle, SlotHandle)
        # A free lane costs nothing; the point is that the number EXISTS to be read.
        assert handle.waited_seconds >= 0.0


def test_burst_mode_reports_the_same_shape():
    """A caller reading `.waited_seconds` must not have to know which limiter it got."""
    with NullRateLimiter().slot("anthropic") as handle:
        assert handle.waited_seconds == 0.0


def test_a_contended_lane_reports_a_real_wait():
    """The whole reason the field exists: when the lane is full, the wait is the story."""
    import threading

    limiter = ModelRateLimiter(max_concurrency=1, min_interval_s=0.0)
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with limiter.slot("anthropic"):
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert holding.wait(timeout=5)

    waited = {}
    def second():
        with limiter.slot("anthropic") as h:
            waited["s"] = h.waited_seconds

    t2 = threading.Thread(target=second, daemon=True)
    t2.start()
    import time as _t
    _t.sleep(0.25)          # the second caller is now blocked on the full lane
    release.set()
    t.join(timeout=5); t2.join(timeout=5)

    assert waited.get("s", 0) >= 0.2, (
        "a caller that queued behind a full lane reported no wait — which is the exact "
        "blindness this exists to remove"
    )


def test_the_guest_stage_records_the_split_and_cannot_break_a_run():
    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    # Wait and work are measured separately, not as one number.
    assert "with limiter.slot(provider) as _slot:" in src
    assert "_work_started = time.monotonic()" in src
    assert "_record_stage_timing(" in src
    # Persisted per run, so a slow run can be read back afterwards rather than watched live.
    assert "'stage_timings'" in src
    # A diagnostic that can fail a build is worse than no diagnostic.
    rec = src[src.index("def _record_stage_timing"):]
    assert "except Exception:" in rec[:rec.index("def _phase")]
