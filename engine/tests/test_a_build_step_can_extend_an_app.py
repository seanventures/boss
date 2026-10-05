"""create_code_task with `extends_run_id` is the app's next build, not a new app.

A workflow's build step calls create_code_task, which branched from main and scaffolded — so a
workflow started for a feature of an existing app built a second app beside it. Given the app's
build, the tool hands the work to the follow-up path (the app's branch, repository and lineage)
and keeps the workflow's own bookkeeping: the run that governs it, so no second outer run opens.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from boss_engine.services import code_task_service as cts
from boss_engine.tools import builtin
from boss_engine.tools.builtin import build_default_registry

PARENT = {"id": "crun_app", "project_id": "p1", "title": "Shift swap board",
          "spec": {"owner": "acme", "repo": "swaps"}, "finalize_ctx": {}}


class _Ctx:
    jobs = None
    settings = None

    @contextmanager
    def begin(self):
        yield object()


@pytest.fixture()
def started(monkeypatch):
    captured: dict = {}

    def follow_up(connection, **kwargs):
        captured.update(kwargs)
        return {"run_id": "crun_next", "root_run_id": "crun_app",
                "base_branch": "boss/code/crun_app", "owner": "acme", "repo": "swaps"}

    monkeypatch.setattr(cts, "start_followup_task", follow_up)
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _r: dict(PARENT))
    monkeypatch.setattr(builtin, "_code_task_in_project",
                        lambda _c, run_id, pid: {"id": run_id} if run_id == "crun_app" else None)
    monkeypatch.setattr(builtin, "_building_agent", lambda *_a: None)
    monkeypatch.setattr(builtin, "_app_build_in_flight", lambda *_a: None)

    def fail_fresh(*_a, **_k):
        raise AssertionError("a fresh build was started for an existing app")

    monkeypatch.setattr(cts, "start_code_task", fail_fresh)
    return captured


def _call(args):
    tool = build_default_registry().get("create_code_task")
    return tool.handler(_Ctx(), project_id="p1", actor="workflow_runner", args=args)


def test_it_continues_the_app_under_the_workflow_that_asked(started):
    out = _call({"title": "Export as CSV", "instructions": "Add a CSV export.",
                 "acceptance_criteria": ["It downloads."], "workflow_run_id": "wfr_1",
                 "repository": "someone/else", "reference_links": ["https://x.test/doc"],
                 "extends_run_id": "crun_app"})
    assert started["parent_run_id"] == "crun_app"
    assert started["workflow_run_id"] == "wfr_1"
    assert started["reference_links"] == ["https://x.test/doc"]
    assert started["title"] == "Shift swap board: Export as CSV"
    assert started["acceptance_criteria"] == ["It downloads.", cts.EXISTING_TESTS_CRITERION]
    assert out["extended_existing_app"] is True
    assert out["extended_from_root"] == "crun_app"
    assert out["base_branch"] == "boss/code/crun_app"
    assert out["repository"] == "acme/swaps"  # the app's, not the argument's
    assert out["run_id"] == "crun_next"


def test_criteria_that_already_promise_the_existing_tests_are_left_alone():
    kept = ["Existing unit tests still pass.", "It downloads."]
    assert cts.with_existing_tests_criterion(kept) == kept


def test_the_composers_own_words_count_as_that_promise():
    """New project's default check says it in plain words; saying it twice reads as two checks."""
    kept = ["A snoozed lead comes back", "Everything that worked before still works"]
    assert cts.with_existing_tests_criterion(kept) == kept
    assert cts.with_existing_tests_criterion(["A snoozed lead comes back"])[-1] == cts.EXISTING_TESTS_CRITERION


def test_a_build_of_the_app_still_on_the_line_is_waited_for_not_adopted(started, monkeypatch):
    """Found walking it: the app's launch-check repair was still building its branch, and
    start_code_task's same-app guard handed THAT run back — the step waited on someone else's
    work as if it were the feature. A workflow step waits; a direct call is told why."""
    from boss_engine.services.workflow_capability_runner import CapabilityWaitError

    monkeypatch.setattr(builtin, "_app_build_in_flight", lambda *_a: "crun_repair")
    with pytest.raises(CapabilityWaitError) as waited:
        _call({"title": "Export", "instructions": "Add a CSV export.", "workflow_run_id": "wfr_1",
               "extends_run_id": "crun_app"})
    assert waited.value.kind == "app_busy" and "crun_repair" in str(waited.value)
    told = _call({"title": "Export", "instructions": "Add a CSV export.", "extends_run_id": "crun_app"})
    assert told["ok"] is False and "crun_repair" in told["error"]
    assert started == {}


def test_an_app_from_another_project_starts_nothing(started):
    out = _call({"title": "x", "instructions": "x", "extends_run_id": "crun_elsewhere"})
    assert out["ok"] is False and "crun_elsewhere" in out["error"]
    assert not started


def test_the_tool_tells_the_model_the_field_exists():
    params = build_default_registry().get("create_code_task").parameters
    assert "extends_run_id" in params["properties"]
