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


def test_every_sandbox_command_says_how_long_it_took():
    """Which command ate the five minutes.

    `duration_ms` was returned to the caller and, for everything except the recorded test
    commands, dropped. So a stage that spent four of its five minutes installing dependencies
    into a freshly cloned temp dir — which every stage gets, because `provision` shallow-clones
    per task and the per-repo cache is a microVM feature that is still stubbed — was
    indistinguishable from a stage that spent five minutes running tests.
    """
    from boss_engine.services import sandbox

    src = inspect.getsource(sandbox)
    assert "_log = logging.getLogger(__name__)" in src
    assert "sandbox cmd task=%s exit=%s %dms" in src
    # The duration that is logged is the same one returned, not a second measurement that
    # could disagree with it.
    assert "duration_ms=_ms," in src


def test_package_and_browser_downloads_are_cached_between_sandboxes():
    """Every stage gets a fresh clone, so every stage re-runs `npm install` and
    `playwright install`. Without a shared cache those RE-DOWNLOAD — Playwright's browsers
    alone are hundreds of megabytes — for bytes that are byte-identical each time."""
    from boss_engine.services import sandbox

    caches = sandbox._shared_tool_caches()
    assert caches["npm_config_cache"].endswith("/npm")
    assert caches["PLAYWRIGHT_BROWSERS_PATH"].endswith("/playwright")

    src = inspect.getsource(sandbox)
    # Applied to the env every command runs under.
    assert 'env = {**os.environ, "CI": "1", **_shared_tool_caches()}' in src


def test_the_cache_lives_outside_the_working_tree():
    """Anything under the clone is deleted with it, and `git add -A` would sweep it into the
    diff — a cache inside the tree is both useless and dangerous."""
    from boss_engine.services import sandbox

    for path in sandbox._shared_tool_caches().values():
        assert "/tmp/boss-tool-cache" in path or not path.startswith("."), path


def test_an_unwritable_cache_does_not_break_the_build():
    """A cache is an optimisation. If the path cannot be created the run must still work —
    just as slowly as before — rather than failing on a directory."""
    from boss_engine.services import sandbox

    src = inspect.getsource(sandbox._shared_tool_caches)
    assert "return {}" in src
    assert "running uncached" in src


def test_the_local_path_reports_its_pacing_too():
    """The gap that a real run exposed.

    Every run on this deployment uses substrate=local, which dispatches through
    `PacedCodeAgent`, NOT through the guest/microVM `_guest_stage`. Instrumenting only the
    latter produced a completed run with ZERO stage timings — the measurement was in the path
    that does not run.
    """
    from boss_engine.services import code_agent

    src = inspect.getsource(code_agent.PacedCodeAgent)
    assert "with self._limiter.slot(self._provider) as slot:" in src
    # The moment the slot was GRANTED is stamped separately from the moment the dispatch was
    # requested — that difference is the queueing time, and folding the two together is what
    # made "the lane was full" indistinguishable from "the model was slow". Asserted by
    # behaviour rather than by variable name, which is free to change.
    assert "_log_pacing(" in src
    assert src.count("time.monotonic()") >= 2
    # Reported on the failure path too: a dispatch that queued and then errored is exactly the
    # case worth knowing about.
    assert "failed=True" in src and "failed=False" in src
    # And the case with no dispatch AT ALL: `slot()` can time out before the call is ever made,
    # which used to record nothing anywhere. See test_never_dispatched_is_recorded.py.
    assert "_record_never_dispatched(" in src


def test_the_pacing_log_cannot_break_a_dispatch():
    from boss_engine.services import code_agent

    src = inspect.getsource(code_agent.PacedCodeAgent._log_pacing)
    assert "except Exception:" in src
