"""BOSS can be asked to change an app that already exists.

Almost all real software work is changing software that already exists. BOSS could only be
asked to build something new: the console's control was "run this task again", which rebuilds
the same app from the same prompt.

The capability was already there and unreachable. When a review deferred a finding, BOSS
spawned a task on the shipped branch, carrying the parent's repo, profile and lineage --
exactly the right thing. It just could not be asked to do it for anything else.

What that cost in practice: continuing work on an app meant identifying its git branch among
ninety-nine of them by matching strings from the built bundle against the source, then
hand-writing base_branch into an API payload. Nobody outside this repository could do it.

These pin the properties that make "change this app" real: the work starts from the app's own
code, keeps its lineage so the preview knows which app it is, and inherits where it lives.
"""

from __future__ import annotations

import pytest

from boss_engine.services import code_task_service as cts


PARENT = {
    "id": "crun_child",
    "project_id": "boss-dogfood",
    "task_id": "tsk_1",
    "title": "ApproveIt — Expense Approval Workflow",
    "spec": {
        "owner": "example-user",
        "repo": "boss-sandbox",
        "profile": "balanced",
        "risk_tier": 1,
        "test_commands": ["npm run verify"],
        "base_branch": "main",
        "model_config": {"default": {"engine": "openai"}},
        "repair_retries": 2,
    },
    "finalize_ctx": {
        "root_run_id": "crun_root",
        "official": True,
        "qa_benchmark": {"id": "approveit", "app_root": "approveit", "official": True},
    },
}


@pytest.fixture()
def started(monkeypatch):
    """Capture what start_code_task would be called with."""
    captured: dict = {}

    def _start(connection, **kwargs):
        captured.update(kwargs)
        return "crun_new"

    monkeypatch.setattr(cts, "start_code_task", _start)
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _r: dict(PARENT))
    return captured


def test_the_change_starts_from_the_apps_own_branch(started):
    """The whole point. Starting from `main` would rebuild, not change."""
    out = cts.start_followup_task(
        None, parent_run_id="crun_child", instructions="save expenses to a database", jobs=None
    )
    assert started["base_branch"] == "boss/code/crun_root"
    assert out["base_branch"] == "boss/code/crun_root"


def test_it_keeps_the_lineage_so_the_preview_knows_which_app_this_is(started):
    """A follow-up spawned as its own root once previewed a different application entirely."""
    cts.start_followup_task(
        None, parent_run_id="crun_child", instructions="add a filter", jobs=None
    )
    fctx = started["finalize_ctx"]
    assert fctx["root_run_id"] == "crun_root"
    assert fctx["parent_run_id"] == "crun_child"
    assert fctx["is_followup"] is True
    assert fctx["official"] is True
    assert fctx["qa_benchmark"]["id"] == "approveit"


def test_it_inherits_where_the_app_lives_and_how_it_is_verified(started):
    """Nobody should retype the repo, and a change must run the app's own checks."""
    cts.start_followup_task(
        None, parent_run_id="crun_child", instructions="add a filter", jobs=None
    )
    assert (started["owner"], started["repo"]) == ("example-user", "boss-sandbox")
    assert started["test_commands"] == ["npm run verify"]
    assert started["profile"] == "balanced"
    assert started["model_config"]["default"]["engine"] == "openai"
    assert started["repair_retries"] == 2


def test_the_agent_is_told_to_build_on_what_is_there(started):
    """Given a bare instruction, a coding agent will happily rewrite the app."""
    cts.start_followup_task(
        None, parent_run_id="crun_child", instructions="save to a database", jobs=None
    )
    text = started["instructions"]
    assert "EXISTING" in text
    assert "Do not rebuild" in text
    assert "save to a database" in text


def test_a_run_with_no_lineage_uses_its_own_branch(monkeypatch):
    """A first run IS the root; there is no parent to inherit from."""
    captured: dict = {}
    monkeypatch.setattr(cts, "start_code_task",
                        lambda connection, **kw: captured.update(kw) or "crun_new")
    lone = dict(PARENT, finalize_ctx={})
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _r: lone)

    cts.start_followup_task(None, parent_run_id="crun_child", instructions="x", jobs=None)
    assert captured["base_branch"] == "boss/code/crun_child"


def test_naming_a_run_that_does_not_exist_says_so(monkeypatch):
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _r: None)
    with pytest.raises(ValueError, match="not found"):
        cts.start_followup_task(None, parent_run_id="nope", instructions="x", jobs=None)


def test_followup_copies_parent_design_so_official_plan_is_not_lost(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cts, "start_code_task",
                        lambda connection, **kw: captured.update(kw) or "crun_new")
    parent = dict(PARENT)
    parent["state"] = {"design": {"plan": "Keep HookWatch. Make verify real.", "plan_ok": True}}
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _r: parent)
    cts.start_followup_task(None, parent_run_id="crun_child", instructions="fix verify", jobs=None)
    assert captured["finalize_ctx"]["inherited_design"]["plan"].startswith("Keep HookWatch")


def test_followup_engine_override_pins_openai(started):
    cts.start_followup_task(
        None,
        parent_run_id="crun_child",
        instructions="fix verify",
        jobs=None,
        engine="openai",
        review_panel_size=5,
    )
    assert started["model_config"]["default"]["engine"] == "openai"
    assert started["finalize_ctx"]["qa_benchmark"]["engine"] == "openai"
    assert started["review_panel_size"] == 5


def test_followup_unknown_engine_is_rejected(started):
    with pytest.raises(ValueError, match="unknown engine"):
        cts.start_followup_task(
            None,
            parent_run_id="crun_child",
            instructions="x",
            jobs=None,
            engine="not-a-lane",
        )


def test_the_branch_rule_lives_in_one_place():
    """Preview, deploy and follow-up all need "where is this app's code"; drift here previewed
    the wrong application once already."""
    assert cts.work_branch_for({"id": "crun_a", "finalize_ctx": {}}) == "boss/code/crun_a"
    assert cts.work_branch_for(
        {"id": "crun_b", "finalize_ctx": {"root_run_id": "crun_a"}}
    ) == "boss/code/crun_a"
