"""A workflow can be run as a TEST with pinned data, and a finished run can be RUN AGAIN FROM A
STEP (#512, services/workflow_test_runs.py).

Each state transition the feature adds is pinned here:

* a pinned step goes pending -> succeeded with its pinned output, `output.pinned = true`, its
  provenance, and a `workflow.step_pinned` event — and is never leased;
* in a test run, a step that could spend or reach outside BOSS goes pending -> failed with reason
  `test_run_refused` (event `workflow.step_test_refused`) and the run stops — unless it is pinned
  or allowed live; the claim and Try again refuse it too;
* a test run is flagged `mode = 'test'` and left out of the run list unless asked for;
* a re-run copies every earlier step's output as a pin with provenance to the source run, and
  is recorded with `rerun_of`; it is refused when the chosen step depends on a step that did not
  complete; and it is confined to the project that owns the source run.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_test_runs as tr
from boss_engine.services import workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _start,
    _steps,
    eng,
    project,
)

WORKER = "worker-t"


def OPEN(plan):
    """The start door, passing: what a ready, clean start returns (no pre-check record)."""
    return None

CHAIN = [
    {"id": "a", "kind": "task", "next": ["b"]},
    {"id": "b", "kind": "task", "next": ["c"]},
    {"id": "c", "kind": "task"},
]


def _publish(c, project_id, steps=CHAIN, entry="a"):
    key = f"tr_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product",
    )
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry, "steps": steps}, actor="test",
    )["id"]


def _test_run(c, project_id, version_id, pins=None, allow_live=None):
    return tr.start_test_run(c, project_id=project_id, actor="test",
                             workflow_version_id=version_id, pins=pins,
                             allow_live=allow_live)


def _claim(c, project_id, run_id):
    return workflows.claim_step(c, project_id=project_id, run_id=run_id, worker_id=WORKER,
                                lease_seconds=600)


def _finish(c, project_id, run_id, step_id, output=None):
    claimed = _claim(c, project_id, run_id)
    assert claimed is not None and claimed["step_id"] == step_id, claimed
    workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, output=output or {step_id: "real"}, evidence=[])


def _events(c, run_id, event_type):
    return c.execute(text("SELECT data FROM events WHERE entity_id=:r AND event_type=:t "
                          "ORDER BY created_at, id"),
                     {"r": run_id, "t": event_type}).scalars().all()


def _run(c, run_id):
    return c.execute(text("SELECT * FROM workflow_runs WHERE id=:r"),
                     {"r": run_id}).mappings().one()


# ── what could spend ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("step, refused", [
    ({"id": "x", "kind": "capability", "binding": {"capability": "report"}}, False),
    ({"id": "x", "kind": "capability", "binding": {"capability": "cost_check"}}, False),
    ({"id": "x", "kind": "capability", "binding": {"capability": "spec_review"}}, False),
    ({"id": "x", "kind": "capability", "binding": {"capability": "build"}}, True),
    ({"id": "x", "kind": "capability", "binding": {"capability": "deploy"}}, True),
    ({"id": "x", "kind": "capability", "binding": {"capability": "notify"}}, True),
    ({"id": "x", "kind": "capability", "binding": {"capability": "no_such_thing"}}, True),
    ({"id": "x", "kind": "task"}, True),
    ({"id": "x", "kind": "skill"}, True),
    ({"id": "x", "kind": "integration"}, True),
    ({"id": "x", "kind": "agent_group"}, True),
    ({"id": "x", "kind": "subworkflow"}, True),
    ({"id": "x", "kind": "condition"}, False),
    ({"id": "x", "kind": "decision"}, False),
])
def test_what_a_test_run_refuses_is_read_from_the_risk_declaration(step, refused):
    assert (tr.effect_of(step) is not None) is refused


def test_a_pin_is_validated_against_the_spec():
    spec = {"entry_step": "a", "steps": [
        {"id": "a", "kind": "task", "next": ["k"]},
        {"id": "k", "kind": "condition"}]}
    with pytest.raises(workflows.WorkflowValidationError, match="no such step"):
        tr.normalise_pins(spec, {"zz": {}}, actor="t")
    with pytest.raises(workflows.WorkflowValidationError, match="cannot be pinned"):
        tr.normalise_pins(spec, {"k": {}}, actor="t")
    with pytest.raises(workflows.WorkflowValidationError, match="JSON object"):
        tr.normalise_pins(spec, {"a": [1, 2]}, actor="t")
    with pytest.raises(workflows.WorkflowValidationError, match="under"):
        tr.normalise_pins(spec, {"a": {"x": "y" * (tr.MAX_PIN_BYTES + 1)}}, actor="t")
    assert tr.normalise_pins(spec, {"a": {"v": 1, "pinned": False}}, actor="t") == {
        "a": {"output": {"v": 1}, "source": {"kind": "person", "actor": "t"}}}
    with pytest.raises(workflows.WorkflowValidationError, match="no step"):
        tr.normalise_allow_live(spec, ["zz"])


# ── a test run ────────────────────────────────────────────────────────────────────────────


def test_a_pinned_step_is_settled_with_its_output_and_never_leased(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run = _test_run(c, project, version, pins={"a": {"answer": 42}, "b": {"more": "x"}},
                        allow_live=["c"])
        run_id = run["id"]
        assert run["mode"] == "test"
        assert run["pins"]["a"]["output"] == {"answer": 42}
        steps = _steps(c, run_id)
        assert steps["a"]["status"] == "succeeded" and steps["b"]["status"] == "succeeded"
        assert steps["a"]["output"]["pinned"] is True
        assert steps["a"]["output"]["answer"] == 42
        assert steps["a"]["output"]["pin"] == {"kind": "person", "actor": "test"}
        pinned = _events(c, run_id, tr.PIN_EVENT)
        assert [e["step_id"] for e in pinned] == ["a", "b"]
        assert not _events(c, run_id, "workflow.step_leased")
        # The pinned output is what later steps read; the pin's own bookkeeping is not.
        context = _run(c, run_id)["context"]
        assert context["answer"] == 42 and "pinned" not in context
        # The step allowed live is the only one that runs, through the ordinary claim.
        assert steps["c"]["status"] == "ready"
        assert _events(c, run_id, tr.LIVE_EVENT)
        _finish(c, project, run_id, "c")
        assert _run(c, run_id)["status"] == "completed"


def test_a_test_run_refuses_an_effectful_step_it_was_not_told_to_run(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run_id = _test_run(c, project, version, pins={"a": {"answer": 1}})["id"]
        steps = _steps(c, run_id)
        assert steps["b"]["status"] == "failed"
        assert steps["b"]["output"]["reason"] == tr.REFUSED_REASON
        assert "test run does not run this step" in steps["b"]["last_error"]
        assert steps["c"]["status"] == "pending"
        refused = _events(c, run_id, tr.REFUSED_EVENT)
        assert [e["step_id"] for e in refused] == ["b"]
        assert not _events(c, run_id, "workflow.step_leased")
        run = _run(c, run_id)
        assert run["status"] == "failed" and "Stopped at" in run["blocked_reason"]
        # Try again is refused for the same reason, not put back in the queue.
        with pytest.raises(workflows.WorkflowError):
            workflows.retry_failed_step(c, project_id=project, run_id=run_id, step_id="b",
                                        actor="test")
        assert _steps(c, run_id)["b"]["status"] == "failed"


def test_the_claim_refuses_a_test_step_that_got_back_to_ready(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run_id = _test_run(c, project, version, pins={"a": {"answer": 1}})["id"]
        # As if a re-check had put it back: ready, run running again.
        c.execute(text("UPDATE workflow_step_runs SET status='ready', output='{}'::jsonb "
                       "WHERE workflow_run_id=:r AND step_id='b'"), {"r": run_id})
        c.execute(text("UPDATE workflow_runs SET status='running' WHERE id=:r"), {"r": run_id})
        assert _claim(c, project, run_id) is None
        assert _steps(c, run_id)["b"]["status"] == "failed"
        assert not _events(c, run_id, "workflow.step_leased")


def test_a_normal_run_is_unchanged(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run = _start(c, project, version, {})
        assert run["mode"] == "normal" and run["pins"] == {} and run["rerun_of"] is None
        assert _steps(c, run["id"])["a"]["status"] == "ready"
        _finish(c, project, run["id"], "a")


def test_a_test_run_is_flagged_and_left_out_of_the_list_unless_asked(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        normal = _start(c, project, version, {})["id"]
        test = _test_run(c, project, version, pins={"a": {}})["id"]
        listed = {r["id"] for r in workflows.list_runs(c, project)}
        assert normal in listed and test not in listed
        everything = {r["id"]: r for r in workflows.list_runs(c, project, include_test=True)}
        assert {normal, test} <= set(everything) and everything[test]["mode"] == "test"
        assert {r["id"] for r in workflows.list_runs(c, project, only_test=True)} == {test}


# ── re-run from a step ────────────────────────────────────────────────────────────────────


def _failed_at_c(c, project):
    version = _publish(c, project)
    run_id = _start(c, project, version, {})["id"]
    _finish(c, project, run_id, "a", {"from_a": 1})
    _finish(c, project, run_id, "b", {"from_b": 2})
    claimed = _claim(c, project, run_id)
    workflows.fail_step(c, project_id=project, step_run_id=claimed["id"], worker_id=WORKER,
                        error="it broke", retryable=False)
    assert _run(c, run_id)["status"] in workflows.TERMINAL_RUN_STATES
    return run_id


def test_a_rerun_copies_earlier_outputs_with_provenance_and_runs_from_the_step(eng, project):
    with eng.begin() as c:
        source = _failed_at_c(c, project)
        source_steps = {r["step_id"]: r for r in c.execute(
            text("SELECT * FROM workflow_step_runs WHERE workflow_run_id=:r"),
            {"r": source}).mappings()}
        rerun = tr.rerun_from_step(c, project_id=project, run_id=source, from_step="c",
                                   actor="test", door=OPEN)
        assert rerun["rerun_of"] == {"run_id": source, "from_step": "c"}
        assert rerun["mode"] == "normal"
        steps = _steps(c, rerun["id"])
        assert steps["a"]["output"]["from_a"] == 1 and steps["a"]["output"]["pinned"] is True
        assert steps["a"]["output"]["pin"] == {
            "kind": "rerun", "run_id": source, "step_run_id": source_steps["a"]["id"],
            "iteration": 0}
        assert steps["b"]["output"]["from_b"] == 2
        assert steps["c"]["status"] == "ready"
        assert [e["step_id"] for e in _events(c, rerun["id"], tr.PIN_EVENT)] == ["a", "b"]
        started = _events(c, source, tr.RERUN_EVENT)
        assert started and started[0]["new_run_id"] == rerun["id"]
        _finish(c, project, rerun["id"], "c")
        assert _run(c, rerun["id"])["status"] == "completed"


def test_a_rerun_from_a_middle_step_runs_it_and_everything_after(eng, project):
    with eng.begin() as c:
        source = _failed_at_c(c, project)
        rerun = tr.rerun_from_step(c, project_id=project, run_id=source, from_step="b",
                                   actor="test", door=OPEN)
        steps = _steps(c, rerun["id"])
        assert steps["a"]["status"] == "succeeded" and steps["b"]["status"] == "ready"
        assert set(rerun["pins"]) == {"a"}


def test_a_rerun_is_refused_when_the_step_depends_on_one_that_did_not_complete(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run_id = _start(c, project, version, {})["id"]
        claimed = _claim(c, project, run_id)
        workflows.fail_step(c, project_id=project, step_run_id=claimed["id"],
                            worker_id=WORKER, error="no", retryable=False)
        with pytest.raises(workflows.WorkflowConflictError, match="did not complete"):
            tr.rerun_from_step(c, project_id=project, run_id=run_id, from_step="c",
                               actor="test")
        # From the failed step itself is fine: nothing before it is missing.
        assert tr.rerun_from_step(c, project_id=project, run_id=run_id, from_step="a",
                                  actor="test", door=OPEN)["pins"] == {}


def test_a_rerun_is_refused_while_the_source_is_still_running(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run_id = _start(c, project, version, {})["id"]
        with pytest.raises(workflows.WorkflowConflictError, match="finished or failed"):
            tr.rerun_from_step(c, project_id=project, run_id=run_id, from_step="b",
                               actor="test")
        with pytest.raises(workflows.WorkflowValidationError):
            _failed = _failed_at_c(c, project)
            tr.rerun_from_step(c, project_id=project, run_id=_failed, from_step="nope",
                               actor="test")


def test_a_rerun_of_a_test_run_keeps_its_later_pins(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        source = _test_run(c, project, version, pins={"a": {"x": 1}, "c": {"z": 3}},
                           allow_live=["b"])["id"]
        _finish(c, project, source, "b", {"y": 2})
        assert _run(c, source)["status"] == "completed"
        rerun = tr.rerun_from_step(c, project_id=project, run_id=source, from_step="b",
                                   actor="test")
        assert rerun["mode"] == "test" and rerun["allow_live"] == ["b"]
        assert rerun["pins"]["a"]["source"]["kind"] == "rerun"
        assert rerun["pins"]["c"]["source"]["kind"] == "person"
        assert _steps(c, rerun["id"])["b"]["status"] == "ready"


def test_both_doors_are_confined_to_the_project_that_owns_the_run(eng, project):
    with eng.begin() as c:
        source = _failed_at_c(c, project)
        other = f"p_{uuid.uuid4().hex[:10]}"
        with pytest.raises(workflows.WorkflowNotFoundError):
            tr.rerun_from_step(c, project_id=other, run_id=source, from_step="c",
                               actor="test")
        version = _run(c, source)["workflow_version_id"]
        with pytest.raises(workflows.WorkflowError):
            _test_run(c, other, version, pins={"a": {}})


def test_a_test_run_is_not_scored_as_production_work(eng, project):
    from boss_engine.services import scorecards

    with eng.begin() as c:
        version = _publish(c, project)
        test = _test_run(c, project, version, pins={"a": {}})["id"]   # stops, failed, at b
        normal = _start(c, project, version, {})["id"]
        claimed = _claim(c, project, normal)
        workflows.fail_step(c, project_id=project, step_run_id=claimed["id"],
                            worker_id=WORKER, error="no", retryable=False)
        assert scorecards._read_workflow_run(c, test) is None
        assert scorecards._read_workflow_run(c, normal) is not None
        step_b = c.execute(text("SELECT id FROM workflow_step_runs WHERE workflow_run_id=:r "
                                "AND step_id='b'"), {"r": test}).scalar()
        assert scorecards._read_step(c, step_b) is None


# ── a normal re-run goes through the start door ───────────────────────────────────────────


def _runs_of(c, project):
    return c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id=:p"),
                     {"p": project}).scalar()


def test_a_normal_rerun_without_the_start_door_is_refused_and_starts_nothing(eng, project):
    with eng.begin() as c:
        source = _failed_at_c(c, project)
        before = _runs_of(c, project)
        with pytest.raises(workflows.WorkflowConflictError, match="start door"):
            tr.rerun_from_step(c, project_id=project, run_id=source, from_step="c",
                               actor="test")
        assert _runs_of(c, project) == before


def test_a_normal_rerun_the_door_refuses_starts_nothing_and_keeps_the_doors_words(
        eng, project):
    class NotReady(Exception):
        pass

    seen = []

    def refusing(plan):
        seen.append(plan)
        raise NotReady("This workflow cannot run here yet: connect a deployment host.")

    with eng.begin() as c:
        source = _failed_at_c(c, project)
        before = _runs_of(c, project)
        with pytest.raises(NotReady, match="cannot run here yet: connect a deployment host"):
            tr.rerun_from_step(c, project_id=project, run_id=source, from_step="c",
                               actor="test", door=refusing)
        assert _runs_of(c, project) == before
        assert not _events(c, source, tr.RERUN_EVENT)
        version = _run(c, source)["workflow_version_id"]
        assert seen == [{"version_id": version, "input": {}, "mode": "normal",
                         "source_run_id": source, "from_step": "c"}]


def test_a_normal_rerun_the_door_passes_starts_and_keeps_its_precheck(eng, project):
    with eng.begin() as c:
        source = _failed_at_c(c, project)
        rerun = tr.rerun_from_step(
            c, project_id=project, run_id=source, from_step="c", actor="test",
            door=lambda plan: {"will_fail": [], "checked_by": "the door"})
        assert rerun["input"][workflows.PRECHECK_KEY] == {"will_fail": [],
                                                          "checked_by": "the door"}
        assert _steps(c, rerun["id"])["c"]["status"] == "ready"


def test_a_test_rerun_keeps_its_own_path_and_never_asks_the_door(eng, project):
    def never(plan):
        raise AssertionError("a test re-run does not go through the start door")

    with eng.begin() as c:
        version = _publish(c, project)
        source = _test_run(c, project, version, pins={"a": {}, "b": {}, "c": {}})["id"]
        rerun = tr.rerun_from_step(c, project_id=project, run_id=source, from_step="c",
                                   actor="test", door=never)
        assert rerun["mode"] == "test"
