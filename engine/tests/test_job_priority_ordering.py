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


# --------------------------------------------------------------------------- #
# The second bypass: an escalation path that outranked every class at once
# --------------------------------------------------------------------------- #
def test_qa_benchmarks_are_the_lowest_band_because_nobody_waits_on_them():
    from boss_engine.services.jobs import JOB_PRIORITY_BULK
    assert JOB_PRIORITY_BULK > JOB_PRIORITY_BACKGROUND > JOB_PRIORITY_REQUESTED \
        > JOB_PRIORITY_INTERACTIVE


def test_benchmark_runs_are_enqueued_at_bulk():
    from apps.api.app import main
    src = inspect.getsource(main)
    assert "job_priority=JOB_PRIORITY_BULK" in src, (
        "a benchmark must yield to everything; it used to outrank everything")


def test_the_metatron_boost_cannot_outrank_interactive_work():
    """It set priority=5 AND priority_score=9000.

    claim_job orders by `priority_score DESC` FIRST, so that single UPDATE put benchmark work
    ahead of every job in the system, permanently, past any priority the table assigns. An
    operator's PRD meeting sat behind two of these for over an hour.
    """
    from boss_engine.services import metatron_command
    src = inspect.getsource(metatron_command.MetatronCommand._reprioritize)
    assert "priority = 5" not in src
    assert "9000" not in src, "priority_score is the lever that bypasses the whole table"
    assert "priority_score" not in src.split("UPDATE jobs")[1].split("WHERE")[0], (
        "the boost must not touch priority_score at all")
    assert "JOB_PRIORITY_BACKGROUND" in src


def test_no_service_invents_a_bare_priority_above_interactive():
    """A number below INTERACTIVE outranks a person, so it must be NAMED and justified.

    Named constants are fine — JOB_PRIORITY_URGENT exists for work whose delay costs money
    (the janitor terminating a paid droplet) or that the operator already approved. A bare
    literal is how both of today's inversions got in: 46-vs-48, and metatron's 5/9000.
    """
    import pathlib
    import re

    services = pathlib.Path("engine/boss_engine/services")
    offenders = []
    for path in services.glob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("--"):
                continue  # prose about a past bug is not a call site
            for m in re.finditer(r"priority\s*=\s*(\d+)", line):
                if int(m.group(1)) < JOB_PRIORITY_INTERACTIVE:
                    offenders.append(f"{path.name}: {stripped[:60]}")
    assert not offenders, (
        "these outrank interactive work with a bare literal; name the band instead:\n  "
        + "\n  ".join(sorted(set(offenders))))


# --------------------------------------------------------------------------- #
# The guard scanned only engine/; two live literals survived in the API layer
# --------------------------------------------------------------------------- #
def test_no_meeting_is_enqueued_at_a_bare_literal_anywhere():
    """I6 was fixed in meeting_service.py and left unfixed in apps/api/app/main.py.

    `test_no_service_invents_a_bare_priority_above_interactive` scans
    engine/boss_engine/services only, and it only fails on values BELOW interactive — so
    `kind="meeting.step", priority=48` in the API layer matched neither condition and sat there
    still ranking an operator's meeting BEHIND batch code tasks at 46, which is the exact
    inversion I6 was about.
    """
    import pathlib
    import re

    offenders = []
    for root in (pathlib.Path("engine/boss_engine"), pathlib.Path("apps/api/app"),
                 pathlib.Path("apps/worker")):
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "meeting.step" in line and re.search(r"priority\s*=\s*\d+", line):
                    offenders.append(f"{path}:{lineno}: {stripped[:70]}")
    assert not offenders, (
        "a meeting enqueued at a bare number is how the ranking inverted; name the band:\n  "
        + "\n  ".join(offenders))


def test_the_bare_literal_sweep_covers_the_api_layer_too():
    """The sweep's ROOTS are the bug: a rule enforced over one directory is a rule the next
    directory does not have."""
    import inspect
    src = inspect.getsource(test_no_meeting_is_enqueued_at_a_bare_literal_anywhere)
    assert "apps/api/app" in src
