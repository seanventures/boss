"""Tell an operator when the MACHINE lost the time, not the pipeline.

On 2026-09-20 a build took 85 minutes that had taken under 20 the day before. The reasonable
conclusion — "BOSS is regressing" — was wrong. The API process froze twice, once for 13m56s,
serving nothing at all, not even /health, because the Mac hosting Docker was thrashing: 21.4 GB
of 22.5 GB swap in use and 23 million pageouts.

Every existing reader in self_observability would have said "healthy" throughout, because they
see the Docker Desktop VM (3.8 GB available, 1 GB swap, 281 GB free) and the VM was fine. The
machine underneath it was not, and nothing inside a container can see that directly.

So this measures whether OUR OWN CODE GOT TO RUN. A loop due every 20 seconds that wakes 14
minutes late was starved, whatever the cause — host swap, VM pause, CPU steal, a closed laptop.
That conclusion survives the container boundary; a memory reading does not.
"""

from __future__ import annotations

from boss_engine.services.self_observability import (
    STALL_THRESHOLD_SECONDS, StallTracker, host_is_degraded, stall_seconds,
)

# The real gaps, measured from the API container's own logs and the sandbox file mtimes.
REAL_STALLS = (836.0, 747.0, 923.0, 710.0)
REAL_ELAPSED = 85 * 60


def _todays_run() -> StallTracker:
    tracker = StallTracker()
    for i, gap in enumerate(REAL_STALLS):
        tracker.observe(20.0, gap, at=float(i * 1000), source="worker.poll")
    return tracker


def test_a_wake_up_on_time_is_not_a_stall():
    assert stall_seconds(20.0, 20.4) == 0.0


def test_ordinary_jitter_is_not_a_stall():
    """A slow query or a busy moment is not what this is looking for."""
    assert stall_seconds(20.0, 20.0 + STALL_THRESHOLD_SECONDS - 1) == 0.0


def test_a_fourteen_minute_freeze_is_a_stall():
    assert stall_seconds(20.0, 836.0) == 816.0


def test_todays_run_is_reported_as_the_hosts_fault():
    verdict = host_is_degraded(_todays_run().since(None), elapsed_seconds=REAL_ELAPSED)
    assert verdict["state"] == "critical"
    assert verdict["not_boss"] is True
    assert verdict["share_of_elapsed"] >= 0.5      # measured: 62%


def test_the_verdict_says_how_much_time_the_machine_took():
    verdict = host_is_degraded(_todays_run().since(None), elapsed_seconds=REAL_ELAPSED)
    assert "3136s" in verdict["detail"] and "longest 903s" in verdict["detail"]


def test_a_healthy_machine_says_so_plainly():
    verdict = host_is_degraded(StallTracker().since(None), elapsed_seconds=1200)
    assert verdict["state"] == "ok"
    assert verdict["stalled_seconds"] == 0.0
    assert "not_boss" not in verdict           # nothing to excuse


def test_one_brief_stall_is_degraded_not_critical():
    tracker = StallTracker()
    tracker.observe(20.0, 90.0, at=1.0)
    assert host_is_degraded(tracker.since(None), elapsed_seconds=3600)["state"] == "degraded"


def test_a_single_long_freeze_is_critical_even_in_a_long_run():
    """Five minutes of not being scheduled is critical however long the run was."""
    tracker = StallTracker()
    tracker.observe(20.0, 320.0, at=1.0)
    assert host_is_degraded(tracker.since(None), elapsed_seconds=86400)["state"] == "critical"


def test_stalls_can_be_attributed_to_one_run():
    """The question is 'how much did THIS run lose', not 'has the machine ever been slow'."""
    tracker = StallTracker()
    tracker.observe(20.0, 600.0, at=100.0)     # before the run started
    tracker.observe(20.0, 400.0, at=900.0)     # during it
    during = tracker.since(500.0)
    assert during["stall_events"] == 1
    assert during["stalled_seconds"] == 380.0


def test_history_is_bounded_so_a_long_lived_process_does_not_grow():
    tracker = StallTracker(keep=5)
    for i in range(40):
        tracker.observe(20.0, 200.0, at=float(i))
    assert len(tracker.recent()) == 5


def test_nonsense_input_is_not_a_stall():
    for expected, actual in ((0.0, 500.0), (-5.0, 500.0), (20.0, 0.0), (20.0, -1.0)):
        assert stall_seconds(expected, actual) == 0.0
