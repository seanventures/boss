"""No automatic remedy stops a code-task attempt that is alive and making progress.

crun_1839849d (HelpDesk build 6, 2026-10-11) was streaming agent turns when BOSS killed it:
the worker's fixed job-runtime ceiling let its lease lapse, the reclaimed job re-entered the
same api for the same run, and the second execution's cleanup — terminate_owner(run_id) —
SIGTERMed the first execution's live agent. Three times; the third was fatal ("claude code
agent exited 143"). Meanwhile the shepherd kept retrying the build's dead predecessor although
a person had already retried that workflow step, and factory_flow called the build stalled
after 25 minutes of a phase whose single agent turn may run 90.

(a) live work is never cancelled, failed, requeued or started twice;
(b) a run with a newer attempt is never retried again;
(c) a phase stall is judged against the configured agent timeout, not a fixed window.
"""
from __future__ import annotations

import inspect
import threading
import time
from types import SimpleNamespace

from boss_engine.services import code_task_service, factory_flow, run_liveness
from boss_engine.services.shepherd_watch import (
    HEALTHY,
    KILLED_BY_PLATFORM,
    RunSnapshot,
    act_now,
    assess,
)

LAPTOP = SimpleNamespace(code_agent_timeout_seconds=5400)


def snap(**over) -> RunSnapshot:
    base = dict(run_id="crun_live", project_id="boss-dogfood", status="running",
                phase="implement", title="HelpDesk: add Login", minutes_running=104.0,
                minutes_since_update=50.0)
    base.update(over)
    return RunSnapshot(**base)


# --------------------------------------------------------------------------------------------
# (a) live work is never stopped
# --------------------------------------------------------------------------------------------

def test_a_long_agent_turn_is_progress_not_a_stall():
    horizon = run_liveness.stall_horizon_seconds(LAPTOP)
    assert horizon == 5400 + run_liveness.GRACE_SECONDS
    # Eighty silent minutes inside one 90-minute implement turn: still alive.
    assert run_liveness.is_progressing(status="running", seconds_since_activity=80 * 60,
                                       horizon_seconds=horizon)
    assert not run_liveness.is_progressing(status="running", seconds_since_activity=horizon + 1,
                                           horizon_seconds=horizon)
    assert not run_liveness.is_progressing(status="failed", seconds_since_activity=5,
                                           horizon_seconds=horizon)
    # Executing in this very process outranks every clock.
    assert run_liveness.is_progressing(status="running", seconds_since_activity=None,
                                       horizon_seconds=horizon, executing=True)


def test_the_horizon_never_drops_below_the_floor():
    assert run_liveness.stall_horizon_seconds(SimpleNamespace(code_agent_timeout_seconds=60)) \
        == run_liveness.MIN_HORIZON_SECONDS
    assert run_liveness.stall_horizon_seconds(None) >= run_liveness.MIN_HORIZON_SECONDS


def test_the_shepherd_never_cancels_fails_or_requeues_a_progressing_run():
    from boss_engine.services.shepherd_watch import WatchVerdict, REDUNDANT_DUPLICATE, ORPHANED

    live = snap(progressing=True, duplicate_of="crun_older", has_live_worker=False,
                minutes_since_heartbeat=40.0)
    for verdict in (
        WatchVerdict(REDUNDANT_DUPLICATE, "dup", action="cancel", autonomous=True, evidence={}),
        WatchVerdict(ORPHANED, "orphan", action="requeue", autonomous=True, evidence={}),
        WatchVerdict(ORPHANED, "fail it", action="fail", autonomous=True, evidence={}),
    ):
        may, why = act_now(verdict, live)
        assert may is False, verdict.action
        assert "progress" in why
    # The same verdicts still act on a run that is NOT progressing.
    quiet = snap(progressing=False, duplicate_of="crun_older")
    dup = WatchVerdict(REDUNDANT_DUPLICATE, "dup", action="cancel", autonomous=True, evidence={})
    assert act_now(dup, quiet)[0] is True


def test_a_redelivered_run_joins_the_live_execution_instead_of_starting_twice():
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []
    joined: list[bool] = []

    def first():
        calls.append("first")
        started.set()
        release.wait(5)
        return {"status": "completed", "done": True}

    def second():  # the redelivery: must never run (its cleanup is what sent the SIGTERM)
        calls.append("second")
        return {"status": "failed"}

    out: dict = {}
    t1 = threading.Thread(target=lambda: out.setdefault("a", run_liveness.run_once("crun_j", first)))
    t1.start()
    assert started.wait(5)
    assert run_liveness.executing_here("crun_j")
    t2 = threading.Thread(target=lambda: out.setdefault(
        "b", run_liveness.run_once("crun_j", second, on_join=lambda: joined.append(True))))
    t2.start()
    time.sleep(0.2)
    release.set()
    t1.join(5)
    t2.join(5)
    assert calls == ["first"]
    assert joined == [True]
    assert out["a"] == out["b"] == {"status": "completed", "done": True}
    assert not run_liveness.executing_here("crun_j")


def test_a_joined_redelivery_sees_the_live_executions_error():
    started, release = threading.Event(), threading.Event()

    def boom():
        started.set()
        release.wait(5)
        raise RuntimeError("agent exited 1")

    errors: list[str] = []

    def call(fn):
        try:
            run_liveness.run_once("crun_e", fn)
        except RuntimeError as e:
            errors.append(str(e))

    t1 = threading.Thread(target=call, args=(boom,))
    t1.start()
    assert started.wait(5)
    t2 = threading.Thread(target=call, args=(lambda: None,))
    t2.start()
    time.sleep(0.1)
    release.set()
    t1.join(5)
    t2.join(5)
    assert errors == ["agent exited 1", "agent exited 1"]


def test_run_code_task_goes_through_the_once_per_process_guard():
    body = inspect.getsource(code_task_service.run_code_task)
    assert "run_liveness.run_once(" in body
    # The cleanup that ends the run's processes lives only inside the guarded execution.
    assert "owned_process.terminate_owner(" not in body
    assert "owned_process.terminate_owner(" in inspect.getsource(code_task_service._run_code_task_once)


# --------------------------------------------------------------------------------------------
# (b) never retry what was already retried
# --------------------------------------------------------------------------------------------

def test_a_failed_run_a_person_already_retried_is_left_alone():
    dead = snap(status="failed", run_id="crun_944194c4", minutes_since_update=1.0,
                run_error="I/O operation on closed file. [substrate=local_subprocess]",
                superseded_by="crun_1839849d")
    verdict = assess(dead)
    assert verdict.klass == HEALTHY
    assert verdict.action == "none"
    assert verdict.evidence["superseded_by"] == "crun_1839849d"


def test_a_failed_run_without_a_successor_is_still_retried():
    verdict = assess(snap(status="failed", run_error="claude code agent exited 143"))
    assert verdict.klass == KILLED_BY_PLATFORM
    assert verdict.action == "retry"


def test_supersession_covers_lineage_step_retry_and_same_work():
    sql = run_liveness.SUPERSEDES_SQL
    assert "root_run_id" in sql and "parent_run_id" in sql
    assert "step_run_id" in sql          # a person's Retry on the workflow step
    assert "work_fingerprint" in sql     # the same work started again
    assert "n.created_at > r.created_at" in sql


def test_every_automatic_retry_path_checks_for_a_newer_attempt():
    from boss_engine.services import shepherd_service

    assert "superseded_by" in inspect.getsource(shepherd_service._retry_run)
    assert "superseded_by" in inspect.getsource(shepherd_service.act)
    assert "progressing" in inspect.getsource(shepherd_service.act)


# --------------------------------------------------------------------------------------------
# (c) phase stall is judged against the configured agent timeout
# --------------------------------------------------------------------------------------------

def flow(**over) -> dict:
    base = {"status": "running", "phase": "implement", "error": "",
            "minutes_since_heartbeat": 1.0,
            "stall_minutes": run_liveness.stall_horizon_seconds(LAPTOP) / 60.0}
    base.update(over)
    return base


def test_an_hour_in_implement_is_not_a_stall_when_one_turn_may_run_ninety_minutes():
    # Build 6 drew a phase_stall at 11:54, 26 minutes into implement.
    assert factory_flow.diagnose_flow(flow(minutes_in_phase=26.0))["klass"] != "phase_stall"
    assert factory_flow.diagnose_flow(flow(minutes_in_phase=60.0))["klass"] != "phase_stall"


def test_recent_agent_turns_are_never_a_phase_stall():
    d = factory_flow.diagnose_flow(flow(minutes_in_phase=300.0, minutes_since_activity=2.0))
    assert d["klass"] != "phase_stall"


def test_silence_past_the_agent_timeout_is_still_a_stall():
    d = factory_flow.diagnose_flow(flow(minutes_in_phase=120.0, minutes_since_activity=110.0))
    assert d["klass"] == "phase_stall"


def test_without_a_configured_horizon_the_old_floor_still_applies():
    d = factory_flow.diagnose_flow({"status": "running", "phase": "implement",
                                    "minutes_in_phase": 30.0, "minutes_since_heartbeat": 1.0})
    assert d["klass"] == "phase_stall"
