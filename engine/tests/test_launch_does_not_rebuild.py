"""Pressing Launch on something already built must not build it again.

BugBox was built, completed, and opened a pull request. Pressing Launch a second time would
have convened a fresh panel, published another PRD, auto-approved it and started another build
-- twenty minutes and several dollars for a second copy of what the operator already had, with
nothing about the click saying that was what it meant.

The existing guard did not help. It asks "is this happening right now", which catches a
double-press and nothing else; once a run finishes, the guard stops applying and the button
silently becomes a spend button.

Building again stays possible. It just has to be asked for, and the asking is what was missing.
"""

from __future__ import annotations

import inspect

from apps.api.app import main as api_main


SOURCE = inspect.getsource(api_main.start_qa_benchmark_app_endpoint)
CODE = "\n".join(ln for ln in SOURCE.splitlines() if not ln.lstrip().startswith("#"))


def test_launch_checks_whether_the_work_already_exists():
    assert "completed_work_for" in CODE


def test_it_answers_with_the_existing_build_instead_of_starting_one():
    assert '"already_built"' in CODE
    # and it returns before anything is convened
    assert CODE.index('"already_built"') < CODE.index("convene_prd_meeting_core(")


def test_building_again_is_possible_but_must_be_asked_for():
    """A guard that cannot be overridden becomes the next complaint."""
    assert '.get("force")' in CODE


def test_the_answer_tells_the_caller_what_to_do():
    """An API that refuses without saying how to proceed just moves the confusion."""
    assert "force=true" in SOURCE


def test_the_lookup_finds_finished_work_not_running_work():
    """The distinction the original guard missed entirely."""
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.completed_work_for)
    assert "'completed'" in source
    # and it reports the build, so the caller can offer it
    assert "code_task_runs" in source
    assert "pull_request_url" in source


def test_the_in_flight_guard_is_still_there_for_double_clicks():
    """The new check answers a different question; it must not replace the old one."""
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.active_duplicate_meeting)
    assert "'running'" in source


def test_the_button_offers_the_existing_build_before_offering_to_repeat_it():
    """The UI half. A response the card ignores changes nothing an operator sees."""
    import pathlib

    ui = pathlib.Path(__file__).resolve().parents[2] / "apps/web/app/code-tasks-ui.tsx"
    text = ui.read_text()
    assert '"existing"' in text
    assert "Build it again" in text
    # The plain press must never force.
    assert "launch(false)" in text
    assert "launch(true)" in text
