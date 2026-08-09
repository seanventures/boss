"""The shepherd, watching work that has not failed yet.

Until now it was a coroner: `sweep()` selects `status IN ('failed','blocked')`, so it reasoned
about runs that had already stopped. Seven identical benchmark runs sat queued and HEALTHY on
the production box — four HookWatch, two BugBox — and nothing looked at them, because nothing
looks at healthy work. By the time a post-mortem can see waste, it has been paid for.

These tests are the classification core: pure snapshots in, verdicts out, no database. The
thresholds are asserted from BOTH sides — a shepherd that cancels slow-but-working runs is
worse than none, because it destroys work that would have landed and teaches an operator to
switch it off.

    PYTHONPATH=engine .venv/bin/python tests/test_shepherd_watch.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services.shepherd_watch import (  # noqa: E402
    HEALTHY, ORPHANED, ORPHAN_LEASE_MINUTES, OVER_LATENCY, REDUNDANT_DUPLICATE,
    ABANDONED, ABANDONED_MINUTES, STALL_MINUTES, STALLED, STARVATION_MINUTES, STARVED,
    RunSnapshot, assess, permitted,
)

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def snap(**kw) -> RunSnapshot:
    base = dict(run_id="crun_b", project_id="p", status="running", phase="implement",
                title="a task", minutes_running=5.0, minutes_since_update=1.0)
    base.update(kw)
    return RunSnapshot(**base)


def main() -> int:
    print("== a healthy run is left alone ==")
    v = assess(snap())
    check("no verdict on normal progress", v.klass == HEALTHY and v.action == "none")
    check("and nothing is permitted", not permitted(v))

    print("== redundant duplicates: the incident that prompted this ==")
    v = assess(snap(work_fingerprint="fp1", duplicate_of="crun_a"))
    check("the later run is classed redundant", v.klass == REDUNDANT_DUPLICATE)
    check("and cancelled without asking", v.action == "cancel" and permitted(v))
    check("the reason names the run it duplicates", "crun_a" in v.reason)
    # The keeper must never cancel itself: the service passes duplicate_of=None for the
    # earliest run, but a snapshot naming itself must be inert too — belt and braces around
    # the one action that destroys work.
    v = assess(snap(run_id="crun_a", work_fingerprint="fp1", duplicate_of="crun_a"))
    check("a run is never its own duplicate", v.klass != REDUNDANT_DUPLICATE)

    print("== orphaned: the failure that never reports itself ==")
    dead = dict(has_live_worker=False, minutes_since_heartbeat=ORPHAN_LEASE_MINUTES + 1,
                job_status="running")
    v = assess(snap(**dead))
    check("a dead lease with no worker is orphaned", v.klass == ORPHANED)
    check("and requeued, not failed", v.action == "requeue" and permitted(v))
    # Queued-but-unclaimed is NOT orphaned. Every run looks workerless for its first seconds,
    # and requeueing those would be an infinite loop of the shepherd fighting the queue.
    v = assess(snap(has_live_worker=False, minutes_since_heartbeat=2.0, job_status="queued",
                    was_leased=False))
    check("a freshly queued run is not orphaned", v.klass == HEALTHY)
    v = assess(snap(has_live_worker=True, minutes_since_heartbeat=ORPHAN_LEASE_MINUTES + 99))
    check("a live worker is never orphaned, however old the job", v.klass == HEALTHY)

    print("== stranded: the job says running and nobody is running it ==")
    # The exact shape goldeneye reported about itself: job present, status 'running', no
    # owner, no heartbeat, 37 days. Keying the remedy off "was it ever leased" answered no
    # and dropped these into "stalled — ask a human" for five weeks. What decides the remedy
    # is whether requeueing can bite, and it bites on status='running'.
    v = assess(snap(phase="coding", minutes_running=53925.0, minutes_since_update=53925.0,
                    has_job=True, job_status="running", was_leased=False,
                    has_live_worker=False, minutes_since_heartbeat=53925.2,
                    minutes_since_any_lease=None))
    check("a job stuck at running with no owner is orphaned", v.klass == ORPHANED)
    check("and requeued, which is an UPDATE that will match",
          v.action == "requeue" and permitted(v))
    check("not left to a human as a stall", v.klass != STALLED)

    print("== starved: a dead worker is not eight unlucky tasks ==")
    # The goldeneye shape: eight runs at status=running / phase=queued, none ever claimed.
    # Classing these ORPHANED would "requeue" them — an UPDATE matching no rows, reported as
    # a fix, every two minutes, forever. Distinguishing never-claimed from lease-lost is the
    # difference between a shepherd and a liar.
    starving = dict(phase="queued", minutes_running=120.0, minutes_since_update=120.0,
                    has_live_worker=False, minutes_since_heartbeat=120.0,
                    has_job=True, job_status="queued",
                    was_leased=False, minutes_since_any_lease=120.0)
    v = assess(snap(**starving))
    check("a never-claimed run with a dead queue is starved", v.klass == STARVED)
    check("it is NOT called orphaned", v.klass != ORPHANED)
    check("and never auto-requeued, because that cannot help",
          v.action == "escalate" and not permitted(v))
    check("the remedy names the real fix", "worker" in v.evidence.get("remedy", ""))
    # A lease that DID exist and went silent is still recoverable by requeueing.
    v = assess(snap(has_live_worker=False, minutes_since_heartbeat=ORPHAN_LEASE_MINUTES + 1,
                    job_status="running", was_leased=True, minutes_since_any_lease=1.0))
    check("a lost lease is still orphaned and requeued",
          v.klass == ORPHANED and v.action == "requeue")
    # A healthy queue that simply has not reached this job yet must be left alone.
    v = assess(snap(minutes_running=STARVATION_MINUTES + 5, has_live_worker=False,
                    minutes_since_heartbeat=1.0, job_status="queued", was_leased=False,
                    minutes_since_any_lease=0.5))
    check("a busy queue that has not got to it yet is healthy", v.klass == HEALTHY)
    v = assess(snap(minutes_running=1.0, has_live_worker=False, minutes_since_heartbeat=1.0,
                    job_status="queued", was_leased=False, minutes_since_any_lease=120.0))
    check("a job queued one minute ago is not starved yet", v.klass == HEALTHY)
    # No lease activity at all comes back as None. Read charitably as "unknown", that turned
    # the loudest possible signal into none at all — twice, on the same production backlog.
    v = assess(snap(minutes_running=120.0, minutes_since_update=120.0, has_live_worker=False,
                    minutes_since_heartbeat=120.0, job_status="queued", was_leased=False,
                    minutes_since_any_lease=None))
    check("never any lease anywhere is starvation, not ignorance", v.klass == STARVED)

    print("== abandoned: the 'Queued — running…' that never moves ==")
    # What the production console actually showed: ten runs auto-refreshing every four
    # seconds, one wedged in `coding` for 53,899 minutes, with no job row behind any of them.
    # Nothing would ever pick them up, and nothing said so.
    v = assess(snap(phase="coding", minutes_running=53899.0, minutes_since_update=53899.0,
                    has_job=False, has_live_worker=False, was_leased=False))
    check("a run with no job is abandoned", v.klass == ABANDONED)
    check("not merely stalled", v.klass != STALLED)
    check("marked failed, autonomously", v.action == "fail" and permitted(v))
    # NOT requeued: that would restart month-old work nobody asked for, on a real budget.
    check("never requeued — relaunching stale work costs money", v.action != "requeue")
    check("and it says how to get the work back if wanted",
          "relaunch" in v.evidence.get("remedy", ""))
    v = assess(snap(has_job=False, minutes_running=ABANDONED_MINUTES - 1,
                    minutes_since_update=1.0))
    check("a run whose job is still being created is left alone", v.klass == HEALTHY)

    print("== stalled: escalated, never cancelled ==")
    v = assess(snap(minutes_since_update=STALL_MINUTES + 1))
    check("no movement for too long is a stall", v.klass == STALLED)
    check("a human decides, because slow and wedged look alike",
          v.action == "escalate" and not permitted(v))
    v = assess(snap(minutes_since_update=STALL_MINUTES - 1))
    check("just under the threshold is left alone", v.klass == HEALTHY)

    print("== latency is measured against this project, not a constant ==")
    slow = dict(minutes_running=100.0, median_minutes=10.0, sample_size=20)
    check("3x the median with real history is flagged",
          assess(snap(**slow)).klass == OVER_LATENCY)
    check("and escalated, not cancelled", not permitted(assess(snap(**slow))))
    check("a project with no history is never flagged",
          assess(snap(minutes_running=100.0, median_minutes=None, sample_size=0)).klass == HEALTHY)
    check("nor one with too few samples to mean anything",
          assess(snap(minutes_running=100.0, median_minutes=10.0, sample_size=2)).klass == HEALTHY)
    check("a slow project's normal run is not sick",
          assess(snap(minutes_running=100.0, median_minutes=90.0, sample_size=20)).klass == HEALTHY)

    print("== certainty beats timing ==")
    # A duplicate that is ALSO stalled must still be cancelled as a duplicate: the fact
    # outranks the judgement, and cancelling it is right for a reason that needs no threshold.
    v = assess(snap(duplicate_of="crun_a", minutes_since_update=STALL_MINUTES + 10))
    check("a stalled duplicate is judged a duplicate", v.klass == REDUNDANT_DUPLICATE)

    print("== the blast radius is a list, not a mood ==")
    from boss_engine.services.shepherd_watch import AUTONOMOUS_ACTIONS, WatchVerdict
    # The blast radius is an explicit list so that widening it is a visible diff, never a
    # side effect of some verdict deciding it feels confident.
    check("the autonomous actions are exactly these three",
          AUTONOMOUS_ACTIONS == frozenset({"cancel", "requeue", "fail"}))
    check("escalate is never autonomous", "escalate" not in AUTONOMOUS_ACTIONS)
    check("an autonomous flag alone is not enough",
          not permitted(WatchVerdict(STALLED, "x", "escalate", True, {})))
    check("nor is a permitted action alone",
          not permitted(WatchVerdict(STALLED, "x", "cancel", False, {})))

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("the shepherd judges running work correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
