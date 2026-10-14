"""A step both paths of a condition reach runs on either path, once.

THE BUG (#550). "If urgent, hotfix then deploy; otherwise deploy" — `on_true: hotfix`,
`on_false: deploy`, `hotfix → deploy`. A condition skipped the first step of the path it did not
take, and a skipped step is never started again, so on "yes" the run hotfixed and COMPLETED
without deploying. Four shipped specpack workflows have that shape: `verification_schedule`
(the reservation path skipped the enqueue), `adversarial_verification_execute` (a refuted claim
was never recorded), `causal_isolation_verification` (an isolated plan whose effect did not
reproduce was never recorded inconclusive) and `surface_ownership_transfer` (an approved
transfer was never applied).

The rule now (`_process_control_step` → `_skip_unless_reachable`): the head of the path not
taken is skipped only when no live path — the one taken, or any step still in flight — can
reach it. Otherwise it stays pending: a live path starts it when it gets there, and a run that
finishes without reaching it records it `path_not_reached`, as it does any step no path took.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from boss_engine.services import workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _start,
    _steps,
    eng,
    project,
)

ROOT = Path(__file__).resolve().parents[2]
WORKER = "test-worker"


def _publish(c, project_id, steps, entry, namespace="product"):
    key = f"rejoin_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace=namespace,
    )
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry, "steps": steps}, actor="test",
    )


def _ready(c, run_id):
    return {r["step_id"] for r in c.execute(
        text("SELECT step_id FROM workflow_step_runs WHERE workflow_run_id=:r "
             "AND status='ready'"), {"r": run_id}).mappings()}


def _finish(c, project_id, run_id, step_id, output=None):
    """Claim `step_id` as a worker would and complete it. A sibling claimed on the way (a
    parallel branch) stays leased, and is completed from its lease when its turn comes."""
    leased = c.execute(text("SELECT id FROM workflow_step_runs WHERE workflow_run_id=:r "
                            "AND step_id=:s AND status='leased'"),
                       {"r": run_id, "s": step_id}).scalar()
    for _ in range(10):
        if leased:
            break
        claimed = workflows.claim_step(c, project_id=project_id, run_id=run_id,
                                       worker_id=WORKER, lease_seconds=600)
        assert claimed is not None, (step_id, _statuses(c, run_id))
        if claimed["step_id"] == step_id:
            leased = claimed["id"]
    assert leased, (step_id, _statuses(c, run_id))
    workflows.complete_step(c, project_id=project_id, step_run_id=leased,
                            worker_id=WORKER, output=output or {step_id: True}, evidence=[])


def _statuses(c, run_id):
    return {k: v["status"] for k, v in _steps(c, run_id).items()}


def _run_status(c, run_id):
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                     {"r": run_id}).scalar()


def _rows_for(c, run_id, step_id):
    return c.execute(text("SELECT count(*) FROM workflow_step_runs WHERE workflow_run_id=:r "
                          "AND step_id=:s"), {"r": run_id, "s": step_id}).scalar()


def _claims_of(c, run_id, step_id):
    return c.execute(text(
        "SELECT count(*) FROM events WHERE entity_id=:r AND event_type='workflow.step_leased' "
        "AND data->>'step_id'=:s"), {"r": run_id, "s": step_id}).scalar()


def _urgent_then_deploy(on_true="hotfix", on_false="deploy"):
    """before → c ? hotfix → deploy : deploy."""
    return [
        {"id": "before", "kind": "task", "next": ["c"]},
        {"id": "c", "kind": "condition",
         "expression": {"path": "context.urgent", "op": "eq", "value": True},
         "on_true": on_true, "on_false": on_false},
        {"id": "hotfix", "kind": "task", "next": ["deploy"]},
        {"id": "deploy", "kind": "task"},
    ]


# ── 1. The yes path reaching the no path's first step runs it ───────────────────────────────

def test_on_yes_the_step_the_yes_path_reaches_runs(eng, project):
    with eng.begin() as c:
        version = _publish(c, project, _urgent_then_deploy(), "before")
        run_id = _start(c, project, version["id"], {})["id"]
        _finish(c, project, run_id, "before", {"urgent": True})
        assert _statuses(c, run_id)["deploy"] == "pending", "deploy skipped before hotfix ran"
        _finish(c, project, run_id, "hotfix")
        _finish(c, project, run_id, "deploy")
        statuses, status = _statuses(c, run_id), _run_status(c, run_id)
        kept = c.execute(text(
            "SELECT data FROM events WHERE entity_id=:r "
            "AND event_type='workflow.step_not_skipped'"), {"r": run_id}).scalars().all()
    assert status == "completed"
    assert statuses == {"before": "succeeded", "c": "succeeded", "hotfix": "succeeded",
                        "deploy": "succeeded"}
    # The audit trail says why the untaken path's first step was not skipped.
    assert len(kept) == 1 and kept[0]["step_id"] == "deploy" and kept[0]["condition"] == "c"


def test_on_no_the_yes_path_first_step_reached_from_the_no_path_runs(eng, project):
    """The mirror image: `on_true` is the step the `on_false` path reaches."""
    with eng.begin() as c:
        version = _publish(c, project, _urgent_then_deploy("deploy", "hotfix"), "before")
        run_id = _start(c, project, version["id"], {})["id"]
        _finish(c, project, run_id, "before", {"urgent": False})
        _finish(c, project, run_id, "hotfix")
        _finish(c, project, run_id, "deploy")
        assert _run_status(c, run_id) == "completed"
        assert _statuses(c, run_id)["deploy"] == "succeeded"


# ── 2. On no it runs once ───────────────────────────────────────────────────────────────────

def test_on_no_the_shared_step_runs_once_and_the_yes_path_is_skipped(eng, project):
    with eng.begin() as c:
        version = _publish(c, project, _urgent_then_deploy(), "before")
        run_id = _start(c, project, version["id"], {})["id"]
        _finish(c, project, run_id, "before", {"urgent": False})
        _finish(c, project, run_id, "deploy")
        steps, status = _steps(c, run_id), _run_status(c, run_id)
        assert _rows_for(c, run_id, "deploy") == 1
        assert _claims_of(c, run_id, "deploy") == 1
        assert _ready(c, run_id) == set()
    assert status == "completed"
    assert steps["deploy"]["status"] == "succeeded"
    assert steps["hotfix"]["status"] == "skipped"
    assert steps["hotfix"]["output"] == {"reason": "condition_not_selected"}


def test_both_paths_at_one_step_runs_it_once(eng, project):
    steps = [s for s in _urgent_then_deploy("deploy", "deploy") if s["id"] != "hotfix"]
    with eng.begin() as c:
        version = _publish(c, project, steps, "before")
        for urgent in (True, False):
            run_id = _start(c, project, version["id"], {})["id"]
            _finish(c, project, run_id, "before", {"urgent": urgent})
            _finish(c, project, run_id, "deploy")
            assert _run_status(c, run_id) == "completed"
            assert _claims_of(c, run_id, "deploy") == 1
            assert _rows_for(c, run_id, "deploy") == 1


# ── 3. A genuinely exclusive branch is still skipped ────────────────────────────────────────

def test_on_yes_an_exclusive_no_branch_is_still_skipped(eng, project):
    steps = [
        {"id": "before", "kind": "task", "next": ["c"]},
        {"id": "c", "kind": "condition",
         "expression": {"path": "context.urgent", "op": "eq", "value": True},
         "on_true": "hotfix", "on_false": "plan"},
        {"id": "hotfix", "kind": "task", "next": ["deploy"]},
        {"id": "plan", "kind": "task", "next": ["deploy"]},
        {"id": "deploy", "kind": "task"},
    ]
    with eng.begin() as c:
        version = _publish(c, project, steps, "before")
        run_id = _start(c, project, version["id"], {})["id"]
        _finish(c, project, run_id, "before", {"urgent": True})
        # Skipped at the condition, not left for the end of the run.
        assert _steps(c, run_id)["plan"]["status"] == "skipped"
        _finish(c, project, run_id, "hotfix")
        _finish(c, project, run_id, "deploy")
        steps_after, status = _steps(c, run_id), _run_status(c, run_id)
        not_skipped = c.execute(text(
            "SELECT count(*) FROM events WHERE entity_id=:r "
            "AND event_type='workflow.step_not_skipped'"), {"r": run_id}).scalar()
    assert status == "completed"
    assert steps_after["plan"]["output"] == {"reason": "condition_not_selected"}
    assert steps_after["deploy"]["status"] == "succeeded"
    assert not_skipped == 0


def test_a_step_reachable_only_through_a_later_untaken_branch_is_skipped_there(eng, project):
    """The causal_isolation shape: the yes path reaches the no head only through a second
    condition. Kept pending at the first; skipped by the second when it goes the other way."""
    steps = [
        {"id": "c1", "kind": "condition",
         "expression": {"path": "input.first", "op": "eq", "value": True},
         "on_true": "work", "on_false": "inconclusive"},
        {"id": "work", "kind": "task", "next": ["c2"]},
        {"id": "c2", "kind": "condition",
         "expression": {"path": "context.reproduced", "op": "eq", "value": True},
         "on_true": "passed", "on_false": "inconclusive"},
        {"id": "passed", "kind": "task", "next": ["emit"]},
        {"id": "inconclusive", "kind": "task", "next": ["emit"]},
        {"id": "emit", "kind": "task"},
    ]
    with eng.begin() as c:
        version = _publish(c, project, steps, "c1")
        run_id = _start(c, project, version["id"], {"first": True})["id"]
        _finish(c, project, run_id, "work", {"reproduced": True})
        kept = c.execute(text(
            "SELECT data FROM events WHERE entity_id=:r "
            "AND event_type='workflow.step_not_skipped'"), {"r": run_id}).scalars().all()
        assert [(k["step_id"], k["condition"]) for k in kept] == [("inconclusive", "c1")]
        assert _steps(c, run_id)["inconclusive"]["output"] == {
            "reason": "condition_not_selected"}
        _finish(c, project, run_id, "passed")
        _finish(c, project, run_id, "emit")
        assert _run_status(c, run_id) == "completed"


# ── 4. The shipped specpack workflows with this shape ───────────────────────────────────────

def _skeleton(name):
    """The shipped workflow's graph, runnable here: every step keeps its id, kind and edges.
    A decision gate becomes a task (a worker completing it stands for the approval), and an
    operation input is dropped — what routes the run is the shape, which is what is under test."""
    spec = yaml.safe_load((ROOT / "packages" / "specpack" / "workflows"
                           / f"{name}.yaml").read_text())
    steps = []
    for step in spec["steps"]:
        step = {k: v for k, v in step.items() if k not in {"input", "question", "guidance"}}
        if step["kind"] == "decision":
            step["kind"] = "task"
        steps.append(step)
    return spec["entry_step"], steps


#: (workflow, how far to drive it: [(ready step, its output)], the step that must run).
SHIPPED = {
    "verification_schedule": [
        ("load_policy", None), ("select_due_claims", None),
        ("deduplicate_schedule", {"spend_required": True}), ("reserve_budget", None),
        ("enqueue_verification_runs", None), ("emit_schedule_event", None)],
    "adversarial_verification_execute": [
        ("acquire_run_lease", None), ("load_claim_and_evidence", {"verifier_independent": True}),
        ("execute_adversarial_challenge", None),
        ("classify_outcome", {"verification_outcome": "refuted"}),
        ("revoke_claim_authority", None), ("record_verification", None),
        ("settle_budget", None), ("emit_verification_event", None)],
    "causal_isolation_verification": [
        ("load_causal_claim", None), ("validate_isolation_plan", {"single_changed_variable": True}),
        ("run_control", None), ("run_intervention", None),
        ("compare_results", {"causal_effect_reproduced": False}),
        ("record_inconclusive", None), ("emit_isolation_event", None)],
    "surface_ownership_transfer": [
        ("load_current_ownership", None), ("validate_transfer_parties", {"approval_required": True}),
        ("approve_transfer", None), ("apply_transfer", {"transfer_applied": True}),
        ("publish_handoff", None), ("emit_transfer_event", None)],
}


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_the_shipped_workflow_runs_its_join_step_on_the_yes_path(eng, project, name):
    entry, steps = _skeleton(name)
    with eng.begin() as c:
        # Shipped specs are BOSS's own (the system namespace), as the specpack loads them.
        version = _publish(c, project, steps, entry, namespace="system")
        run_id = _start(c, project, version["id"], {})["id"]
        for step_id, output in SHIPPED[name]:
            _finish(c, project, run_id, step_id, output)
        statuses, status = _statuses(c, run_id), _run_status(c, run_id)
    assert status == "completed", statuses
    ran = {step_id for step_id, _ in SHIPPED[name]}
    assert {s for s, v in statuses.items() if v == "succeeded"} >= ran
    # Everything else is a control step that ran, or a branch the run did not take.
    for step_id, value in statuses.items():
        assert value in {"succeeded", "skipped"}, (step_id, value)
