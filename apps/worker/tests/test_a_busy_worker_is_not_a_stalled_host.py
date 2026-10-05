"""A worker running a job is busy, not stalled (stability spec C5).

The stall clock timed the whole loop, so a two-hour build was reported as the host refusing to
schedule BOSS for two hours: 35 `host.stalled` events on boss-1, each one a job finishing, on an
idle machine. Only the idle wait has a known length; only its lateness is the host's.
"""
from __future__ import annotations

from boss_engine.services.self_observability import StallTracker
from boss_worker import main as worker_main


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.late_once = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds + self.late_once
        self.late_once = 0.0


def test_a_long_job_is_not_a_stall():
    clock = FakeClock()
    tracker = StallTracker()
    # The job: 120 s of work in the cycle, then an on-time idle wait.
    clock.t += 120.0
    late = worker_main.observe_idle_wait(tracker, 20.0, stopping=lambda: False,
                                         sleep=clock.sleep, clock=clock)
    assert late == 0.0
    assert tracker.recent() == []


def test_a_late_idle_wait_is_a_stall_of_about_its_lateness():
    clock = FakeClock()
    tracker = StallTracker()
    clock.late_once = 120.0                      # the host froze inside one sleep
    late = worker_main.observe_idle_wait(tracker, 20.0, stopping=lambda: False,
                                         sleep=clock.sleep, clock=clock)
    assert 100.0 <= late <= 121.0
    assert tracker.recent()[0]["source"] == "worker.idle_wait"


def test_a_stop_request_cuts_the_wait_short_without_reading_as_a_stall():
    clock = FakeClock()
    tracker = StallTracker()
    calls = {"n": 0}

    def stopping() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    late = worker_main.observe_idle_wait(tracker, 20.0, stopping=stopping,
                                         sleep=clock.sleep, clock=clock)
    assert late == 0.0


def test_the_loop_no_longer_times_the_cycle():
    import inspect

    body = inspect.getsource(worker_main.main)
    assert "_now - _last_cycle" not in body
    assert "observe_idle_wait(" in body
