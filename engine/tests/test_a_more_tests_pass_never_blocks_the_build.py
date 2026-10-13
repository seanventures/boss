"""Pass 2 (More tests) adds coverage on the same PR, and never blocks pass 1.

Operator, 2026-10-13: a build ships a working app with a lean suite and its preview first;
a "More tests" pass then adds tests for what was built (edge cases, error paths, API contracts,
the browser tests pass 1 deferred) as its own commits on the same branch and PR, shown as its
own step. If it finds a real bug it fixes it and says so. It never holds up pass 1: a failed
pass 2 completes its step with the failure said, and the run carries on with pass 1's build.
This is the ONE exception to "failed is terminal for the step" (test_a_failed_step_stops_the_run).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import build_scope
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import CAPABILITIES
from boss_engine.services.workflow_catalogue import CATALOGUE

from .test_a_failed_step_stops_the_run import _insert_awaited, _tick_through_retries, _two_step
from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    _steps,
    eng,
    project,
)


def _ids(key: str) -> list[str]:
    spec = next(w for w in CATALOGUE if w["key"] == key)
    return [s["id"] for s in spec["steps"]]


@pytest.mark.parametrize("key", ["standard_product_delivery", "minor_feature", "major_feature"])
def test_more_tests_follows_the_preview_and_runs_beside_the_deploy(key):
    ids = _ids(key)
    assert ids.index("build") < ids.index("preview") < ids.index("more_tests")
    spec = next(w for w in CATALOGUE if w["key"] == key)
    fork = next(s for s in spec["steps"] if s["kind"] == "parallel")
    assert "more_tests" in fork["branches"]


def test_no_deploy_reads_more_tests_but_its_deploy_again_does():
    """The operator, 2026-10-13: no deploy waits for P2, in any environment."""
    for spec in CATALOGUE:
        for step in spec["steps"]:
            wired = (step.get("binding") or {}).get("inputs") or {}
            if (step.get("binding") or {}).get("capability") != "deploy":
                continue
            reads_pass_2 = "more_tests" in str(wired.get("run_id"))
            assert reads_pass_2 == bool(wired.get("pass_2_of")), (spec["key"], step["id"])


def test_it_is_an_awaited_build_pass_and_advisory():
    cap = CAPABILITIES["more_tests"]
    assert cap.is_async and cap.tool is None and cap.label == "More tests"
    assert runner._AWAIT_KINDS["more_tests"][0] == "code_task"
    assert "more_tests" in runner._ADVISORY_CAPABILITIES


def test_pass_2_lifts_pass_1s_limits_and_never_tests_a_connector():
    brief = runner._more_tests_brief()
    assert build_scope.MORE_TESTS_MARKER in brief and "BUGS FIXED" in brief
    assert "connector" in brief or "sign-in, payments" in brief
    scope = build_scope.decide("HelpDesk", "Playwright e2e please\n" + brief, ["c"])
    assert scope["more_tests"] and scope["e2e"] and not scope["e2e_deferred"]
    token = build_scope.set_scope(scope)
    try:
        assert g.test_budget(["c"], 20) == 40
    finally:
        build_scope._SCOPE.reset(token)


def test_a_failed_more_tests_pass_completes_its_step_and_the_run_goes_on(eng, project,
                                                                         monkeypatch):
    awaited = f"crun_more_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    monkeypatch.setattr(runner, "_more_tests_step", lambda *a, **k: {
        "capability": "more_tests", "tool": "extend_code_task", "status": "extending",
        "base_run_id": "crun_pass_one", "run_id": awaited})
    with eng.begin() as c:
        run = _two_step(c, project, "more_tests")
        _tick_through_retries(eng, c, project, run["id"], times=2)
        assert _steps(c, run["id"])["s"]["status"] == "waiting"
        _insert_awaited(c, project, runner._AWAIT_TABLES["code_task"][0], awaited, "failed")
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["s"]["status"] == "succeeded", steps["s"]
        out = steps["s"]["output"]
        assert out["advisory_failed"] is True and out["code_task_run_id"] == "crun_pass_one"
        assert "carries on" in out["note"]
        assert steps["after"]["status"] in ("ready", "running", "succeeded", "waiting")
        c.execute(text("DELETE FROM code_task_runs WHERE id=:i"), {"i": awaited})
