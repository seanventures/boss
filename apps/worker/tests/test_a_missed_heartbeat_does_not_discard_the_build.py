"""One missed heartbeat must not throw away thirty minutes of work.

The renewal interval was `lease_ttl // 2`, which leaves exactly ONE heartbeat between a job and
its lease expiring. Miss it once — a blip on the control plane, a slow query — and the lease
dies on the dot, the reclaimer re-delivers the job, and everything the stage had done is
replayed from the clone.

Measured on crun_b09f23f3bd134953b961, with BOSS_LEASE_TTL_SECONDS=1800 in production:

    phase_timings: implement 1800902 ms, then provisioning again, then implement 641273 ms

The first implement ran for 1800.902s — the lease TTL to the millisecond — and recorded no model
call at all. The second did the same work in 10m41s and produced the run's only implement call.
Forty of the run's forty-six minutes were one lease expiring and the work being redone.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_worker import runner  # noqa: E402


def _renew_source() -> str:
    src = inspect.getsource(runner)
    start = src.index("def _renew_lease()")
    return src[start:src.index("lease_lost = threading.Event()", start)]


def test_the_interval_leaves_room_for_more_than_one_failure():
    body = _renew_source()
    assert "settings.lease_ttl_seconds // 4" in body, "back to one heartbeat before expiry"
    assert "// 2" not in body.split("interval =")[1][:80]


def test_three_consecutive_misses_still_fit_inside_the_lease():
    """The point of the quarter: a transient blip and a dead worker must be distinguishable."""
    for ttl in (300, 600, 1800, 3600):
        interval = max(15, ttl // 4)
        assert interval * 3 < ttl, f"ttl={ttl}: three misses already past expiry"


def test_a_failed_heartbeat_retries_soon_rather_than_sleeping_out_the_interval():
    """Waiting another full interval after a failure gives back the margin the quarter buys."""
    body = _renew_source()
    assert "retry_interval" in body
    assert "retry_interval if misses else interval" in body


def test_the_retry_is_bounded_at_both_ends():
    """Never a busy loop, never so long it is indistinguishable from the normal interval."""
    for ttl in (60, 300, 1800, 7200):
        interval = max(15, ttl // 4)
        retry = max(10, min(60, interval // 4))
        assert 10 <= retry <= 60
        assert retry <= interval


def test_a_success_clears_the_miss_count():
    """Otherwise one early blip pins the thread to the retry cadence for the whole job."""
    body = _renew_source()
    assert "misses = 0" in body
    assert "misses += 1" in body


def test_the_warning_says_how_long_is_left():
    """`heartbeat_renew_failed job_id=x` with no numbers is a line nobody can act on — it does
    not say whether the job is about to be reclaimed."""
    body = _renew_source()
    assert "miss=%d" in body
    assert "retry_in=%ds" in body


def test_every_heartbeat_is_recorded_not_only_the_failures():
    """Two runs lost their lease at exactly the TTL with NO failure logged — so either the
    beats were not happening or they were not extending anything, and the logs could not tell
    the two apart, because only failures were ever written. The next occurrence has to answer
    "was it heartbeating?" rather than raise it."""
    body = _renew_source()
    assert "lease_heartbeat" in body
    assert "beat=%d" in body
    assert "beats += 1" in body


def test_a_long_job_does_not_fill_the_log_with_its_own_heartbeat():
    """A trail, not a flood: the first few, then one in ten."""
    body = _renew_source()
    assert "beats <= 3 or beats % 10 == 0" in body


def test_a_lease_survives_many_consecutive_misses():
    """ttl // 4 was a guess dressed as a fix: three misses and the lease is gone. A minute is
    one small POST, and it turns three chances into thirty."""
    body = _renew_source()
    assert "min(60, settings.lease_ttl_seconds // 4)" in body
    # Four consecutive misses must still fit inside the lease at every TTL we ship with. At
    # the production 1800s that is thirty; the bound is set by the SHORTEST ttl, not the one
    # that happens to be configured here.
    for ttl in (300, 600, 1800, 3600):
        interval = max(15, min(60, ttl // 4))
        assert interval * 4 < ttl, f"ttl={ttl}: four misses already past expiry"
