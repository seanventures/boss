"""Task Shepherd — watching work that is STILL RUNNING.

Design: docs/product/TASK_SHEPHERD_DESIGN.md (this extends §11 past Phase 3)

The shepherd until now was a coroner. `sweep()` selects `status IN ('failed','blocked')`, so
it reasoned beautifully about work that had already stopped and could not see a run in
trouble. Everything expensive happens before that point:

  * seven identical benchmark runs, queued together, each a full budgeted pipeline that could
    produce nothing the first was not already producing. All seven healthy, so nothing had a
    failure to classify — and the existing `DUPLICATE_EXECUTION` class means a lease
    reassigned mid-run, a different problem that happens to share the word;
  * a run whose worker died holding the lease: the row says `running` forever, and nothing
    reclaims it, because nothing marked it failed;
  * a run wedged in one phase for an hour while its budget drains.

None of those are failures yet. All of them are waste an attentive human would catch in
seconds. This module is that attention.

The split from `task_shepherd.py` is deliberate and identical in spirit: the classification
here is PURE — it takes a snapshot of facts and returns a verdict and a bounded action. It
cannot query, spend, or cancel. The service layer gathers the facts and carries out what this
module permits, which is what keeps an "intelligent" watcher from being an unbounded one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# What can be wrong with a run that has not failed
# --------------------------------------------------------------------------- #
# Deliberately NOT reusing the failure taxonomy in task_shepherd.py. Those classes describe
# how a run ENDED; these describe how one is GOING. Sharing the numbering would make a
# reasoning record ambiguous about which question it answered.
HEALTHY = 0
REDUNDANT_DUPLICATE = 1     # identical work already in flight, started earlier
ORPHANED = 2                # no worker holds it and its lease is long gone
STALLED = 3                 # alive, but the same phase for far too long
OVER_LATENCY = 4            # running far longer than this kind of work normally takes
STARVED = 5                 # never claimed by anyone — nothing is consuming the queue
ABANDONED = 6               # no job exists at all: nothing will ever pick this up

WATCH_NAMES = {
    HEALTHY: "healthy",
    REDUNDANT_DUPLICATE: "redundant duplicate",
    ORPHANED: "orphaned — no worker, lease expired",
    STALLED: "stalled in one phase",
    OVER_LATENCY: "over expected latency",
    STARVED: "starved — no worker is consuming the queue",
    ABANDONED: "abandoned — no job exists to carry it",
}

# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
# Generous on purpose. A shepherd that interrupts slow-but-working runs is worse than none:
# it destroys work that would have landed, and it teaches an operator to switch it off. Every
# number here is "no legitimate run has ever taken this long", not "runs usually finish by".
#
# A governed pipeline runs design → tests → implement → review → PR. The longest legitimate
# single phase observed is the implement stage on a large task, tens of minutes.
STALL_MINUTES = 45.0
# The lease TTL is 300s and the worker heartbeats within it. Six times that is unambiguous:
# no live worker renews a lease for half an hour and then comes back.
ORPHAN_LEASE_MINUTES = 30.0
# A run whose job row has vanished. Given generously: a job is created moments after the run
# row, so anything older than this with no job never had one or lost it permanently.
ABANDONED_MINUTES = 15.0
# A queue nobody is draining. Shorter than the orphan threshold because this is not a lost
# run, it is a dead platform: if nothing anywhere has been claimed in ten minutes while work
# is waiting, no amount of patience helps.
STARVATION_MINUTES = 10.0
# Latency is judged against measured history, never a constant — a project whose tasks take
# an hour is not sick. Only flagged with enough samples to mean something.
LATENCY_MULTIPLE = 3.0
LATENCY_MIN_SAMPLES = 5


@dataclass(frozen=True)
class RunSnapshot:
    """Everything the verdict is allowed to depend on. Gathered by the service layer."""

    run_id: str
    project_id: str
    status: str
    phase: str
    title: str
    minutes_running: float
    minutes_since_update: float
    work_fingerprint: Optional[str] = None
    # The oldest in-flight run sharing this fingerprint, if any. Not "some duplicate exists":
    # the shepherd must cancel the LATER one, and to know it is later it needs the winner.
    duplicate_of: Optional[str] = None
    # Job/lease facts. `has_live_worker` is False when nothing owns the job at all.
    has_live_worker: bool = True
    minutes_since_heartbeat: Optional[float] = None
    # Does a job row exist at all? Distinct from "not claimed": a run whose job is gone can
    # never progress, no matter how long anyone waits.
    has_job: bool = True
    # Was this job EVER claimed? The distinction decides whether requeueing can help at all:
    # a job whose worker died is recoverable by putting it back; a job nobody ever took is
    # not, because there is nothing to put it back to.
    was_leased: bool = True
    # Minutes since ANY job in this project was claimed. The queue's pulse, not this run's.
    minutes_since_any_lease: Optional[float] = None
    # Measured baseline for comparable finished runs in this project.
    median_minutes: Optional[float] = None
    sample_size: int = 0


@dataclass(frozen=True)
class WatchVerdict:
    """What is wrong, how sure we are, and the ONE action permitted in response."""

    klass: int
    reason: str
    action: str                 # "none" | "cancel" | "requeue" | "fail" | "escalate"
    autonomous: bool            # may the shepherd do it without asking?
    evidence: dict[str, Any]

    @property
    def name(self) -> str:
        return WATCH_NAMES.get(self.klass, "unknown")


def assess(snap: RunSnapshot) -> WatchVerdict:
    """Judge one in-flight run. Pure: same snapshot, same verdict, always.

    Ordered by certainty, and the order matters. A redundant duplicate is decided from the
    existence of an older identical run — a fact, not an inference — so it is checked before
    anything time-based, which is always a judgement about "too long".
    """
    ev: dict[str, Any] = {
        "phase": snap.phase,
        "minutes_running": round(snap.minutes_running, 1),
        "minutes_since_update": round(snap.minutes_since_update, 1),
    }

    # 1. Redundant duplicate — certain, and the only one worth cancelling on sight.
    #
    # Nothing this run can produce differs from what the earlier one is already producing:
    # same project, same repo, same branch, same instructions. Cancelling the LATER one is
    # safe in a way cancelling the earlier one is not — the earlier has more work banked, and
    # something may already be watching its id.
    if snap.duplicate_of and snap.duplicate_of != snap.run_id:
        return WatchVerdict(
            REDUNDANT_DUPLICATE,
            f"identical work is already running as {snap.duplicate_of}; this run was started "
            f"afterwards and can only repeat it",
            action="cancel", autonomous=True,
            evidence={**ev, "duplicate_of": snap.duplicate_of,
                      "work_fingerprint": snap.work_fingerprint},
        )

    # 2. Orphaned — the row says running and nothing is running it.
    #
    # This is the failure that never reports itself: the worker died holding the lease, so no
    # error was ever written, and the run sits at `running` until a human notices. Requeue
    # rather than fail: the work was never done, and the platform lost it, not the task.
    stale_lease = (snap.minutes_since_heartbeat is not None
                   and snap.minutes_since_heartbeat >= ORPHAN_LEASE_MINUTES)
    if not snap.has_live_worker and stale_lease and snap.was_leased:
        return WatchVerdict(
            ORPHANED,
            f"no worker holds this run and its lease has been silent for "
            f"{snap.minutes_since_heartbeat:.0f} minutes — the executor died, the task did not",
            action="requeue", autonomous=True,
            evidence={**ev, "minutes_since_heartbeat": snap.minutes_since_heartbeat},
        )

    # 3. Abandoned — there is no job, so there is no future.
    #
    # Checked BEFORE starvation, because "waiting for a worker" presupposes something in the
    # queue to be picked up. A run with no job is not waiting; there is nothing to wait for,
    # and describing it as a starved queue would send someone to restart a worker that would
    # have nothing to do.
    #
    # The state the console shows as "Queued — running…", auto-refreshing every four seconds,
    # forever. Ten of these were sitting on the production box, some since July, one wedged
    # in `coding` for 53,899 minutes. Nothing was ever going to happen to them: the job that
    # would have carried the work does not exist.
    #
    # Marked FAILED rather than requeued, deliberately. Requeueing would start month-old work
    # nobody asked for again and spend a real budget on it; the truthful action is to stop
    # claiming it is running. Relaunching it is one click away if anyone still wants it.
    if not snap.has_job and snap.minutes_running >= ABANDONED_MINUTES:
        return WatchVerdict(
            ABANDONED,
            f"no job exists to carry this run and it has said `{snap.phase}` for "
            f"{snap.minutes_running:.0f} minutes — it cannot progress and never will",
            action="fail", autonomous=True,
            evidence={**ev, "remedy": "marked failed; relaunch it if the work is still wanted"},
        )

    # 4. Starved — waiting for a worker that is not coming.
    #
    # The distinction from ORPHANED is the whole point, and getting it wrong is worse than
    # not checking. A job nobody ever claimed cannot be fixed by requeueing it: there is
    # nothing to put back, and the UPDATE matches no rows. Reported as "acted", that is a
    # shepherd claiming to fix something every two minutes while nothing changes — the exact
    # dishonesty this component exists to prevent.
    #
    # Never autonomous, because the remedy is not to this task. Someone has to start a worker.
    # No lease activity AT ALL reads as None. That is not missing evidence, it is evidence of
    # absence: nothing in this project has ever been claimed. Skipping the check on None is
    # what kept ten runs — queued 37 days, never touched — coming back as "stalled, ask a
    # human" instead of "nobody is consuming this queue". The loudest possible signal arrived
    # as no signal, twice, because a null was read charitably.
    starved = (snap.minutes_since_any_lease is None
               or snap.minutes_since_any_lease >= STARVATION_MINUTES)
    if (snap.has_job and not snap.was_leased and starved
            and snap.minutes_running >= STARVATION_MINUTES):
        # None here means "nothing was EVER claimed", which reads worse than a number, not
        # better — and formatting it as one crashes.
        pulse = ("nothing in this project has EVER been claimed"
                 if snap.minutes_since_any_lease is None
                 else f"nothing here has been claimed for "
                      f"{snap.minutes_since_any_lease:.0f} minutes")
        return WatchVerdict(
            STARVED,
            f"queued {snap.minutes_running:.0f} minutes and never claimed; {pulse} — the "
            f"worker is not consuming the queue",
            action="escalate", autonomous=False,
            evidence={**ev, "minutes_since_any_lease": snap.minutes_since_any_lease,
                      "remedy": "start or restart the worker; requeueing cannot help"},
        )

    # 5. Stalled — alive, but nothing has moved.
    #
    # NOT autonomous. A wedged phase and a genuinely slow one look identical from here, and
    # the difference is worth a human glance rather than a cancelled hour of work.
    if snap.minutes_since_update >= STALL_MINUTES:
        return WatchVerdict(
            STALLED,
            f"stuck in `{snap.phase}` with no state change for "
            f"{snap.minutes_since_update:.0f} minutes",
            action="escalate", autonomous=False, evidence=ev,
        )

    # 6. Over latency — measured against this project's own history, never a constant.
    if (snap.median_minutes and snap.sample_size >= LATENCY_MIN_SAMPLES
            and snap.minutes_running >= snap.median_minutes * LATENCY_MULTIPLE):
        return WatchVerdict(
            OVER_LATENCY,
            f"running {snap.minutes_running:.0f} minutes against a median of "
            f"{snap.median_minutes:.0f} over {snap.sample_size} comparable runs",
            action="escalate", autonomous=False,
            evidence={**ev, "median_minutes": snap.median_minutes,
                      "sample_size": snap.sample_size},
        )

    return WatchVerdict(HEALTHY, "progressing normally", action="none",
                        autonomous=False, evidence=ev)


# Actions the shepherd may take on its own, and nothing else. Mirrors AUTONOMOUS_CLASSES in
# shepherd_service: the blast radius is a list you can read, not a property of the reasoning.
AUTONOMOUS_ACTIONS = frozenset({"cancel", "requeue", "fail"})


def permitted(verdict: WatchVerdict) -> bool:
    """May the shepherd carry this out unattended?

    Both conditions, deliberately: a verdict must be marked autonomous AND its action must be
    on the list. One flag is a single point of failure for spending someone's money.
    """
    return verdict.autonomous and verdict.action in AUTONOMOUS_ACTIONS
