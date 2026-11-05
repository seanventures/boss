"""Import a repo → "What should we add?" (2026-11-05): the run names the repository but builds onto
no BOSS app, so neither the panel nor the coder heard the repo already holds an app — the PRD
described a new product and the build could write a parallel app beside the real one. Both are
now told to change the existing codebase."""
from boss_engine.services import workflow_capability_runner as runner


def test_a_repository_with_no_boss_app_is_said_to_be_existing_code():
    note = runner._existing_code_note({"repository": "acme/helpdesk", "directive": "Add SLA timers"})
    assert "acme/helpdesk" in note
    assert "EXISTING codebase" in note
    assert "do not start a parallel app" in note
    assert "existing tests must keep passing" in note


def test_a_build_onto_a_boss_app_keeps_its_own_note():
    # extends_run_id has the app's own, more precise note (its branch and name).
    assert runner._existing_code_note({"repository": "acme/helpdesk", "extends_run_id": "crun_1"}) == ""


def test_no_repository_says_nothing():
    assert runner._existing_code_note({"directive": "Build a todo app"}) == ""
    assert runner._existing_code_note({}) == ""


def test_the_panel_and_the_build_both_use_it():
    import inspect

    source = inspect.getsource(runner._perform_capability)
    assert source.count("_existing_code_note(") >= 2, "panel objective and build brief both carry it"
