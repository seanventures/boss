"""Interactive work must outrank work the system chose to do by itself.

The defect this locks down, measured on goldeneye:

    code.task     priority = 46   (batch build work)
    meeting.step  priority = 48   (an operator watching the screen)

`claim_job` orders by `priority ASC`, so LOWER runs sooner — the ranking was exactly backwards.
It stayed invisible while nothing produced batch work in bulk. Then a shepherd sweep queued
eleven code tasks, the operator's PRD meeting sat behind them and never started, and the same
jobs loaded the API hard enough that the console began reporting "API returned 404".

The priorities were magic numbers invented per call site — 40, 45, 46, 48, 80 across six
services — with no shared table and no statement of what the ordering meant. Nobody could see
the whole ranking in one place, so nobody could see it was inverted.
"""

import inspect

from boss_engine.services.code_task_service import retry_code_task, start_code_task
from boss_engine.services.jobs import (
    JOB_PRIORITY_BACKGROUND,
    JOB_PRIORITY_INTERACTIVE,
    JOB_PRIORITY_REQUESTED,
)


def test_lower_number_runs_sooner_is_the_documented_rule():
    from boss_engine.services import jobs as jobs_mod
    src = inspect.getsource(jobs_mod)
    assert "priority ASC" in src
    assert "LOWER number runs SOONER" in src, "the ordering rule must be stated where it is set"


def test_a_person_waiting_outranks_work_they_asked_for_and_walked_away_from():
    assert JOB_PRIORITY_INTERACTIVE < JOB_PRIORITY_REQUESTED


def test_work_the_system_chose_ranks_last():
    assert JOB_PRIORITY_REQUESTED < JOB_PRIORITY_BACKGROUND


def test_a_meeting_now_outranks_a_code_task_which_is_the_inversion_that_broke_it():
    """The exact live numbers: code.task 46 ahead of meeting.step 48."""
    assert JOB_PRIORITY_INTERACTIVE < 46, (
        "a meeting must not queue behind batch code tasks again")


def test_meeting_jobs_use_the_interactive_constant_not_a_literal():
    from boss_engine.services import meeting_service
    src = inspect.getsource(meeting_service)
    assert "JOB_PRIORITY_INTERACTIVE" in src
    assert "priority=48" not in src, "a literal here is how the ranking drifted in the first place"


def test_shepherd_retries_are_background_not_the_same_as_operator_work():
    from boss_engine.services import shepherd_service
    src = inspect.getsource(shepherd_service)
    assert "job_priority=JOB_PRIORITY_BACKGROUND" in src, (
        "an automatic repair must never queue ahead of a person")


def test_code_tasks_default_to_requested_so_an_operators_build_is_not_demoted():
    src = inspect.getsource(start_code_task)
    assert "JOB_PRIORITY_REQUESTED" in src
    assert "priority=46" not in src


def test_priority_is_threaded_through_retry_so_provenance_survives():
    """Provenance decides, not kind: the same code.task is REQUESTED or BACKGROUND by caller."""
    assert "job_priority" in inspect.signature(retry_code_task).parameters
    assert "job_priority" in inspect.signature(start_code_task).parameters
