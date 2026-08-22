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
