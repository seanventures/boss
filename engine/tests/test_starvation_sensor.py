"""Starvation is the absence of progress, and nothing was watching for absence.

Every other check in the shepherd is failure-triggered: it needs an error string, a failed
gate, a red health check. A job that is simply never claimed produces none of those. A PRD
meeting sat queued for two hours behind benchmark jobs boosted past every priority band, and
every check in the system reported green the whole time.

The sensor works on JOBS rather than code_task_runs, and that is the point: jobs are where
every kind of work meets — meetings, code tasks, previews, deployments, discovery, agent
briefs. A sensor built on code_task_runs would have covered exactly the one kind that was not
stuck.
"""

import inspect

from boss_engine.services import shepherd_service
from boss_engine.services.jobs import (
    JOB_PRIORITY_BULK,
    JOB_PRIORITY_INTERACTIVE,
)
from boss_engine.services.task_shepherd import (
    CLASS_NAMES,
    REMEDIES,
    WORK_STARVED,
)


def test_the_taxonomy_has_a_class_for_work_that_is_merely_waiting():
    assert CLASS_NAMES[WORK_STARVED] == "work queued but never claimed"


def test_remedies_are_free_and_ordered_least_destructive_first():
    order = [r.id for r in REMEDIES[WORK_STARVED]]
    assert order == ["demote_blockers", "cancel_wedged_blocker", "escalate"]
    for remedy in REMEDIES[WORK_STARVED]:
        assert remedy.prior_cost_usd == 0.0, "unblocking a queue must never cost money"


def test_it_demotes_the_blocker_rather_than_promoting_the_starved_job():
    """Promotion is an arms race — the next starved job has to outrank this one.

    That is exactly how the ranking got inverted: each caller nudged its own work up until
    benchmark jobs sat at priority_score 9000, above every band at once.
    """
    demote = REMEDIES[WORK_STARVED][0]
    assert demote.action == "demote_blocking_jobs"
    assert "lower band" in demote.describe


def test_the_sensor_reads_jobs_so_it_covers_every_kind_of_work():
    src = inspect.getsource(shepherd_service.sweep_starved_work)
    body = src.split('"""')[2]  # skip the docstring, which discusses the rejected design
    assert "FROM jobs" in body
    assert "code_task_runs" not in body, (
        "a sensor built on code_task_runs covers exactly the one kind that was not stuck")


def test_a_blocker_only_counts_when_it_outranks_the_waiting_work():
    """Something more important running first is a queue working correctly, not a fault."""
    src = inspect.getsource(shepherd_service.sweep_starved_work)
    assert "outranks" in src
    assert "best_waiting" in src


def test_a_priority_score_is_treated_as_outranking_everything():
    """priority_score is the first sort key, so any value beats every band."""
    src = inspect.getsource(shepherd_service.sweep_starved_work)
    assert 'blocker["priority_score"] is not None' in src


def test_wedged_is_generous_because_real_work_runs_for_hours():
    assert shepherd_service.WEDGED_AFTER_MINUTES >= 60, (
        "a real code task legitimately runs over an hour; cancelling it would be the bug")
    assert shepherd_service.STARVED_AFTER_MINUTES <= 30


def test_the_sensor_runs_before_the_queue_depth_guard():
    """A deep queue is exactly the condition in which something is starving.

    Skipping the check when the queue is deep would blind the sensor in the only situation it
    exists for.
    """
    src = inspect.getsource(shepherd_service.autopilot)
    assert src.index("sweep_starved_work") < src.index("queue_depth")


def test_the_autopilot_reports_what_it_unstarved():
    src = inspect.getsource(shepherd_service.autopilot)
    assert '"unstarved": unstarved' in src


def test_bands_make_the_starving_case_expressible():
    """The whole point: a meeting outranks a benchmark, so the sensor has a correct target."""
    assert JOB_PRIORITY_INTERACTIVE < JOB_PRIORITY_BULK


def test_a_wedged_run_is_detected_by_no_progress_not_by_elapsed_time():
    """The failure that wasted an hour: one llm_call, zero files, 53 minutes in run_tests.

    The sandbox command timeout is 120 SECONDS, so nothing was executing — but by elapsed
    time the job was young, and WEDGED_AFTER_MINUTES would not have touched it for two hours.
    A long task that is still calling models is working; one that has gone quiet is not.
    """
    from boss_engine.services.shepherd_service import (
        NO_PROGRESS_MINUTES, WEDGED_AFTER_MINUTES, _made_no_progress,
    )
    assert NO_PROGRESS_MINUTES < WEDGED_AFTER_MINUTES, (
        "quiet must be caught long before the elapsed-time backstop")
    assert 10 <= NO_PROGRESS_MINUTES <= 45, (
        "short enough to catch a dead run, long enough that a slow stage is not killed")

    src = inspect.getsource(_made_no_progress)
    assert "llm_calls" in src, "progress means model calls, not wall clock"
    assert "max(created_at)" in src


def test_the_sweep_checks_progress_before_giving_up_on_elapsed_time():
    src = inspect.getsource(shepherd_service.sweep_starved_work)
    assert "_made_no_progress" in src
    assert src.index("WEDGED_AFTER_MINUTES") < src.index("_made_no_progress"), (
        "elapsed time first (cheap), then the progress query")


def test_a_process_killed_by_the_os_is_infrastructure_not_a_mystery():
    """`claude code agent exited -9` — SIGKILL, almost always the OOM killer.

    A web build runs npm install, vite and playwright inside the agent's process tree and the
    kernel takes the whole thing. The taxonomy matched `exited 143` (SIGTERM) but NOT `-9`, so
    an OOM-killed run fell through to UNKNOWN, escalated to a human, and sat there. The machine
    killed it; the task did nothing wrong; re-running is the correct remedy.
    """
    from boss_engine.services.task_shepherd import (
        CLASS_NAMES, INFRASTRUCTURE, REMEDIES, UNKNOWN, classify,
    )
    real = "claude code agent exited -9: [substrate=local_subprocess]"
    assert classify(real) == INFRASTRUCTURE, CLASS_NAMES[classify(real)]
    assert classify(real) != UNKNOWN

    for variant in ("exited 137", "process killed by signal 9", "SIGKILL", "exited 143"):
        assert classify(variant) == INFRASTRUCTURE, variant

    # …and the class must offer something automatic, or classifying it changes nothing.
    assert [r.id for r in REMEDIES[INFRASTRUCTURE] if r.action], (
        "infrastructure must have a remedy that acts, not only escalate")


def test_starting_work_is_opt_in_but_sensors_are_not():
    """Bounded autonomous spending is still autonomous spending.

    Turned on for one afternoon the autopilot opened repair tasks for historical failures
    nobody had asked about, retried a build the operator was already retrying by hand — two
    runs of the same app, neither aware of the other — and left a 33-minute auto-repair holding
    the only worker slot while the operator's actual work queued behind it. All of it inside
    its own bounds.

    The free half must keep running: closing rows that claim to be working and unsticking a
    starved queue cost nothing and are the whole point of watching.
    """
    from boss_engine.config import Settings

    assert Settings().shepherd_autopilot_enabled is False, "must default to off"

    src = inspect.getsource(shepherd_service.autopilot)
    gate = src.index("shepherd_autopilot_enabled")
    for free in ("sweep_starved_work", "sweep_abandoned_runs", "sweep_stale_previews"):
        assert src.index(free) < gate, f"{free} costs nothing and must run regardless"
    assert src.index("act(engine, settings, jobs, run_id") > gate, (
        "starting work must be behind the gate")


def test_a_gated_tick_says_why_rather_than_looking_idle():
    src = inspect.getsource(shepherd_service.autopilot)
    assert "BOSS_SHEPHERD_AUTOPILOT_ENABLED is false" in src
