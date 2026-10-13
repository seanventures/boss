"""Every workflow that builds code gets its More tests pass — catalogue, forked or drafted.

Operator, 2026-10-13: pass 1 builds the app with a lean P0/P1 suite and its PR and preview come
first; pass 2 ("More tests", P2) extends the same branch and PR and never blocks pass 1. It was
wired by hand into three catalogue workflows, so the other builders and every custom workflow
never got pass 2. The rule now lives once in the engine (services/two_pass.py), applied where a
version becomes a run's steps (workflows._published_spec) and to the catalogue at import.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import build_scope, two_pass, workflows
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_catalogue import CATALOGUE, entry_spec

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures by name


def _cap(step):
    return (step.get("binding") or {}).get("capability")


def _builds(spec):
    return [s for s in spec["steps"] if _cap(s) == "build"]


def _after(spec, step_id):
    """The step ids that follow `step_id`, in order, along `next` (catalogue: straight lines)."""
    by_id = {s["id"]: s for s in spec["steps"]}
    out, cur = [], by_id[step_id]
    while cur.get("next"):
        cur = by_id[cur["next"][0]]
        out.append(cur["id"])
    return out


BUILDERS = [e for e in CATALOGUE if _builds({"steps": e["steps"]})]


def test_the_catalogue_workflows_that_build_are_the_ones_expected():
    assert {e["key"] for e in BUILDERS} == {
        "standard_product_delivery", "minor_feature", "major_feature", "fast_fix",
        "backend_service_setup", "ux_certification", "security_hardening", "data_migration",
        "compliance_release", "third_party_integration", "deprecation_sunset"}


@pytest.mark.parametrize("entry", BUILDERS, ids=lambda e: e["key"])
def test_every_catalogue_build_is_followed_by_its_more_tests_pass(entry):
    """Unless the workflow opts out (`more_tests: false`), as published and as a run sees it."""
    if entry.get("more_tests") is False:
        pytest.skip("opted out")
    for spec in (entry_spec(entry), workflows._published_spec(entry_spec(entry))):
        assert two_pass.builds_without_more_tests(spec) == [], entry["key"]
        for build in _builds(spec):
            later = _after(spec, build["id"])
            caps = {s["id"]: _cap(s) for s in spec["steps"]}
            mt = next(i for i in later if caps[i] == "more_tests")
            # The PR and the preview come first: pass 2 is after any preview, before shipping.
            if "preview" in [caps[i] for i in later]:
                pv = next(i for i in later if caps[i] == "preview")
                assert later.index(pv) < later.index(mt)
            if "deploy" in [caps[i] for i in later]:
                dp = next(i for i in later if caps[i] == "deploy")
                assert later.index(mt) < later.index(dp)
                # What ships is the last pass's branch, never the build's alone.
                assert spec["steps"][[s["id"] for s in spec["steps"]].index(dp)][
                    "binding"]["inputs"]["run_id"]["from_step"] in (mt, "plrr")
        # Applying the rule again adds nothing.
        assert two_pass.with_more_tests(spec) == spec


def test_no_catalogue_entry_wires_more_tests_by_hand():
    import inspect

    from boss_engine.services import workflow_catalogue

    assert '_step("more_tests"' not in inspect.getsource(workflow_catalogue)


def test_a_hotfix_gets_a_lighter_pass_2():
    fast = next(e for e in CATALOGUE if e["key"] == "fast_fix")
    mt = next(s for s in fast["steps"] if _cap(s) == "more_tests")
    assert mt["binding"]["inputs"]["depth"] == two_pass.LIGHT
    assert "MORE TESTS PASS (LIGHT)" in runner._more_tests_brief(light=True)
    scope = build_scope.decide("fix", "Playwright\n" + runner._more_tests_brief(light=True), [])
    assert scope["more_tests"] and scope["more_tests_light"] and not scope["e2e"]
    token = build_scope.set_scope(scope)
    try:
        assert g.test_budget(["c"], 20) == 20  # pass 1's budget, not twice it
    finally:
        build_scope._SCOPE.reset(token)


def test_pass_1_writes_p0_p1_and_pass_2_writes_p2():
    clause = g._test_scope_clause(["a", "b"])
    assert "P0" in clause and "P1" in clause and "sign-in" in clause
    token = build_scope.set_scope(build_scope.decide("x", runner._more_tests_brief(), []))
    try:
        assert "P2" in g._test_scope_clause(["a", "b"])
        assert "At most 40 NEW test cases" in g._test_scope_clause(["a"], 20)
    finally:
        build_scope._SCOPE.reset(token)
    assert "P2" in runner._more_tests_brief()


# ── A custom workflow ──────────────────────────────────────────────────────────────────────────

def _custom(**top):
    return {
        "entry_step": "build", **top,
        "steps": [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {
                 "title": {"from_input": "title"}, "instructions": {"from_input": "directive"}}},
             "next": ["ship"]},
            {"id": "ship", "kind": "capability",
             "binding": {"capability": "deploy", "inputs": {
                 "run_id": {"from_step": "build", "field": "code_task_run_id"}}},
             "next": []},
        ],
    }


def test_a_custom_workflow_gets_pass_2_when_it_is_resolved_for_a_run():
    spec = workflows._published_spec(_custom())
    ids = [s["id"] for s in spec["steps"]]
    assert ids == ["build", "more_tests", "ship"]
    by_id = {s["id"]: s for s in spec["steps"]}
    assert by_id["build"]["next"] == ["more_tests"] and by_id["more_tests"]["next"] == ["ship"]
    assert by_id["ship"]["binding"]["inputs"]["run_id"] == {
        "from_step": "more_tests", "field": "code_task_run_id"}
    assert "depth" not in by_id["more_tests"]["binding"]["inputs"]


def test_a_custom_workflow_can_opt_out_or_ask_for_a_light_pass():
    assert [s["id"] for s in workflows._published_spec(_custom(more_tests=False))["steps"]] == [
        "build", "ship"]
    one_off = _custom()
    one_off["steps"][0]["more_tests"] = False
    assert len(workflows._published_spec(one_off)["steps"]) == 2
    light = workflows._published_spec(_custom(more_tests="light"))
    assert next(s for s in light["steps"] if s["id"] == "more_tests")[
        "binding"]["inputs"]["depth"] == "light"


def test_each_of_two_builds_gets_its_own_pass_2():
    spec = _custom()
    spec["steps"][0]["next"] = ["build_2"]
    spec["steps"].insert(1, {"id": "build_2", "kind": "capability",
                             "binding": {"capability": "build",
                                         "inputs": dict(spec["steps"][0]["binding"]["inputs"])},
                             "next": ["ship"]})
    out = workflows._published_spec(spec)
    assert [s["id"] for s in out["steps"]] == [
        "build", "more_tests", "build_2", "build_2_more_tests", "ship"]


def test_a_custom_run_walks_the_inserted_step_and_an_older_run_keeps_its_shape(eng, project):
    from .test_a_failed_step_stops_the_run import _publish, _start

    with eng.begin() as c:
        _, version = _publish(c, project, f"two_pass_{uuid.uuid4().hex[:6]}",
                              _custom()["steps"], "build")
        run = _start(c, project, version["id"], {"title": "t", "directive": "d"})
        rows = c.execute(text("SELECT step_id FROM workflow_step_runs WHERE workflow_run_id=:r"),
                         {"r": run["id"]}).scalars().all()
        assert set(rows) == {"build", "more_tests", "ship"}
        assert [s["id"] for s in workflows._run_spec(c, run["id"])["steps"]] == [
            "build", "more_tests", "ship"]
        # A run started before the rule has no More tests row: it keeps its version's shape.
        c.execute(text("DELETE FROM workflow_step_runs WHERE workflow_run_id=:r "
                       "AND step_id='more_tests'"), {"r": run["id"]})
        old = workflows._run_spec(c, run["id"])
        assert [s["id"] for s in old["steps"]] == ["build", "ship"]
        assert old["steps"][1]["binding"]["inputs"]["run_id"]["from_step"] == "build"
