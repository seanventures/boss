"""A run never completes past a failed step nobody has decided about.

THE INCIDENT (review of 534a1b9, 2026-10-02). "A failed step stops the run" had just landed —
research that died on boss-1 had been recorded succeeded and the run walked on into a panel and
a build — and an independent reviewer found the same lie one level up. With two failed parallel
branches, pressing "Skip and proceed" on one set the run running and released the skipped step;
the join could not release (its other branch was still dead-lettered), nothing was active, and
`_maybe_finish_run` COMPLETED the run, recording the join and every step after it as
`path_not_reached`:

    {'fork': 'succeeded', 'b': 'deadlettered', 'a': 'skipped', 'j': 'skipped',
     'after': 'skipped'}   run: completed

The rule enforced here, for every state a step can stop a run in (`STOPPED_STEP_STATES`) and for
both of the operator's answers (skip, and retry-then-succeed): while ANY step is still stopped,
the run is stopped at it — never completed, and its remaining steps never written off.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _publish,
    _start,
    _steps,
    eng,
    project,
)

WORKER = "test-worker"


def _fork_join(c, project_id, *, chain_b: bool = False):
    """fork → (a, b) → join j → after. With `chain_b`, b → b2 → j, so a step that finishes
    while the run is stopped has a next step of its own that must not be lost."""
    steps = [
        {"id": "fork", "kind": "parallel", "branches": ["a", "b"], "join": "j"},
        {"id": "a", "kind": "task", "next": ["j"], "max_attempts": 1},
        {"id": "b", "kind": "task", "next": ["b2" if chain_b else "j"], "max_attempts": 1},
        {"id": "j", "kind": "join", "wait_for": ["a", "b2" if chain_b else "b"],
         "next": ["after"]},
        {"id": "after", "kind": "task"},
    ]
    if chain_b:
        steps.insert(3, {"id": "b2", "kind": "task", "next": ["j"]})
    _, version = _publish(c, project_id, f"fj_{uuid.uuid4().hex[:6]}", steps, "fork")
    return _start(c, project_id, version["id"], {})


def _claim(c, project_id, run_id, step_id):
    """Claim until `step_id` is the one leased. Other ready steps are left leased — a worker
    that is still busy with them — which is exactly the concurrency the bug needed."""
    for _ in range(10):
        claimed = workflows.claim_step(c, project_id=project_id, run_id=run_id,
                                       worker_id=WORKER, lease_seconds=600)
        if claimed is None:
            break
        if claimed["step_id"] == step_id:
            return claimed
    row = c.execute(text("SELECT id, status FROM workflow_step_runs "
                         "WHERE workflow_run_id=:r AND step_id=:s"),
                    {"r": run_id, "s": step_id}).mappings().first()
    assert row and row["status"] in {"leased", "running"}, (step_id, row)
    return {"id": row["id"], "step_id": step_id}


def _stop(c, project_id, run_id, step_id, state):
    """Make `step_id` stop the run in `state`, through the path that really produces it."""
    claimed = _claim(c, project_id, run_id, step_id)
    if state == "deadlettered":
        workflows.fail_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, error=f"{step_id} broke", retryable=False)
    else:  # "failed": awaited work that died
        workflows.park_step_awaiting(c, project_id=project_id, step_run_id=claimed["id"],
                                     worker_id=WORKER,
                                     output={"await": {"kind": "code_task", "id": "crun_x"}})
        workflows.force_fail_waiting_step(c, project_id=project_id,
                                          step_run_id=claimed["id"],
                                          error=f"{step_id} broke")


def _succeed(c, project_id, run_id, step_id):
    claimed = _claim(c, project_id, run_id, step_id)
    workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, output={step_id: True}, evidence=[])


def _run(c, run_id):
    return c.execute(text("SELECT status, blocked_reason, error FROM workflow_runs "
                          "WHERE id=:r"), {"r": run_id}).mappings().one()


def _answer(c, project_id, run_id, step_id, action):
    if action == "skip":
        workflows.skip_failed_step(c, project_id=project_id, run_id=run_id, step_id=step_id,
                                   actor="ceo", reason="going on without it")
    else:
        workflows.retry_failed_step(c, project_id=project_id, run_id=run_id,
                                    step_id=step_id, actor="ceo")
        _succeed(c, project_id, run_id, step_id)


STATES = list(workflows.STOPPED_STEP_STATES)
ACTIONS = ["skip", "retry"]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("state", STATES)
def test_answering_one_of_two_failed_branches_leaves_the_run_stopped_at_the_other(
        eng, project, state, action):
    """The reviewer's probe, for every stopped state and both answers. The run must stop at the
    branch nobody answered — saying so — with the join and `after` still pending, not written
    off. Answering that branch too then completes the run, with every step really run."""
    with eng.begin() as c:
        run = _fork_join(c, project)
        run_id = run["id"]
        _claim(c, project, run_id, "a")              # both branches are now in flight
        _stop(c, project, run_id, "a", state)
        _stop(c, project, run_id, "b", state)

        _answer(c, project, run_id, "a", action)
        steps, stopped = _steps(c, run_id), _run(c, run_id)
        assert stopped["status"] in STATES, f"the run went {stopped['status']} past b"
        assert stopped["status"] != "completed"
        assert steps["b"]["status"] == state
        assert "j" not in steps or steps["j"]["status"] == "pending", steps.get("j")
        assert "after" not in steps or steps["after"]["status"] == "pending", steps.get("after")
        assert (stopped["blocked_reason"] or "").startswith("Stopped at b: b broke."), stopped

        _answer(c, project, run_id, "b", "retry")
        _succeed(c, project, run_id, "after")
        steps, done = _steps(c, run_id), _run(c, run_id)

    assert done["status"] == "completed"
    assert steps["j"]["status"] == "succeeded"
    assert steps["after"]["status"] == "succeeded"
    assert steps["a"]["status"] == ("skipped" if action == "skip" else "succeeded")


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("state", STATES)
def test_work_that_finished_while_the_run_was_stopped_still_hands_on(eng, project, state, action):
    """b was still running when a failed and stopped the run. b then finished — succeeded, but
    with the run stopped its next step was never started. After the operator answers a, that
    next step (b2) must run: otherwise the join waits for ever, or the run "completes" around it.
    """
    with eng.begin() as c:
        run = _fork_join(c, project, chain_b=True)
        run_id = run["id"]
        b = _claim(c, project, run_id, "b")
        _stop(c, project, run_id, "a", state)
        workflows.complete_step(c, project_id=project, step_run_id=b["id"], worker_id=WORKER,
                                output={"b": True}, evidence=[])
        assert _run(c, run_id)["status"] in STATES
        assert "b2" not in _steps(c, run_id) or _steps(c, run_id)["b2"]["status"] == "pending"

        _answer(c, project, run_id, "a", action)
        assert _steps(c, run_id)["b2"]["status"] == "ready", "b's next step was lost"
        _succeed(c, project, run_id, "b2")
        _succeed(c, project, run_id, "after")
        steps, done = _steps(c, run_id), _run(c, run_id)

    assert done["status"] == "completed"
    assert all(steps[s]["status"] == "succeeded" for s in ("b", "b2", "j", "after")), steps


@pytest.mark.parametrize("action", ACTIONS)
def test_answering_a_failed_step_on_a_paused_run_keeps_it_paused(eng, project, action):
    """Skip used to set the run `running` unconditionally, so skipping a step silently un-paused
    a run somebody had paused on purpose. The answer is recorded; the run waits for Resume, and
    Resume then carries the decision through."""
    with eng.begin() as c:
        run = _fork_join(c, project)
        run_id = run["id"]
        _succeed(c, project, run_id, "b")
        _stop(c, project, run_id, "a", "deadlettered")
        # A stopped run is not pausable (it is terminal), so pause it the way an operator can:
        # pause first, while the failure is still in flight.
        c.execute(text("UPDATE workflow_runs SET status='paused' WHERE id=:r"), {"r": run_id})

        if action == "skip":
            workflows.skip_failed_step(c, project_id=project, run_id=run_id, step_id="a",
                                       actor="ceo", reason="not needed")
        else:
            workflows.retry_failed_step(c, project_id=project, run_id=run_id, step_id="a",
                                        actor="ceo")
        assert _run(c, run_id)["status"] == "paused"
        assert "after" not in _steps(c, run_id) or _steps(c, run_id)["after"]["status"] == "pending"

        workflows.resume_run(c, project, run_id, "ceo")
        if action == "retry":
            _succeed(c, project, run_id, "a")
        _succeed(c, project, run_id, "after")
        assert _run(c, run_id)["status"] == "completed"
