"""An advisory step that fails to START completes with the failure said; the run goes on.

BOSSAPP-540, found by the pre-check (lifecycle.advisory_start) on a HelpDesk run. Pass 2 runs
[more_tests] | [deploy, monitor, report, smoke_test] joined on `all`. More tests was advisory
only once its work was running (`resume_awaited` → `_complete_advisory_failure`): a failure
while STARTING it, out of attempts, went through `workflows.fail_step`, which did not ask, so the
step was dead-lettered, the run stopped, and the deploy beside it lost the run it belonged to.

Now `fail_step` completes an advisory step out of attempts the way the runner completes one
whose work failed: `succeeded`, `advisory_failed`, the cause on `last_error` and in a
`workflow.step_advisory_failed` event. A retry that is still possible is still a retry, and a
non-advisory step failing still stops the run (test_a_failed_step_stops_the_run).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

from .test_a_failed_step_stops_the_run import _required
from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _publish,
    _start,
    _steps,
    eng,
    project,
)

WORKER = "test-worker"


def _pass_two(c, project_id, *, advisory_binding: dict, max_attempts: int = 1):
    """fork → (m: the advisory step | d: a deploy stand-in) → join j on `all` → after."""
    _, version = _publish(c, project_id, f"adv_{uuid.uuid4().hex[:6]}", [
        {"id": "fork", "kind": "parallel", "branches": ["m", "d"], "join": "j"},
        {"id": "m", "kind": "capability", "binding": advisory_binding, "next": ["j"],
         "max_attempts": max_attempts},
        {"id": "d", "kind": "task", "next": ["j"], "max_attempts": 1},
        {"id": "j", "kind": "join", "wait_for": ["m", "d"], "success_policy": "all",
         "next": ["after"]},
        {"id": "after", "kind": "task"},
    ], "fork")
    return _start(c, project_id, version["id"], {})


def _more_tests() -> dict:
    return {"capability": "more_tests", "inputs": _required("more_tests")}


def _claim(c, project_id, run_id, step_id):
    """Claim until `step_id` is leased; the other branch stays leased, still in flight."""
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


def _run(c, run_id):
    return c.execute(text("SELECT status, blocked_reason FROM workflow_runs WHERE id=:r"),
                     {"r": run_id}).mappings().one()


def _events(c, run_id, event_type):
    return c.execute(text("SELECT data FROM events WHERE entity_id=:r AND event_type=:t"),
                     {"r": run_id, "t": event_type}).scalars().all()


def test_the_advisory_set_is_the_runners():
    assert workflows.ADVISORY_CAPABILITIES == runner._ADVISORY_CAPABILITIES


@pytest.mark.parametrize("binding", [
    pytest.param(_more_tests(), id="more_tests"),
    pytest.param({"capability": "preview", "inputs": {**_required("preview"), "advisory": True}},
                 id="automatic_preview"),
])
def test_an_advisory_step_that_fails_to_start_completes_and_the_deploy_beside_it_goes_on(
        eng, project, binding):
    with eng.begin() as c:
        run = _pass_two(c, project, advisory_binding=binding)
        run_id = run["id"]
        deploy = _claim(c, project, run_id, "d")          # the deploy is in flight
        m = _claim(c, project, run_id, "m")
        workflows.fail_step(c, project_id=project, step_run_id=m["id"], worker_id=WORKER,
                            error="the consent for this run never asked for extend_code_task",
                            retryable=False)

        steps, stopped = _steps(c, run_id), _run(c, run_id)
        assert stopped["status"] == "running", stopped
        assert not stopped["blocked_reason"]
        assert steps["m"]["status"] == "succeeded"
        # SAID, NEVER SILENT: the cause, the note, and an event of its own.
        assert "extend_code_task" in steps["m"]["last_error"]
        out = steps["m"]["output"]
        assert out["advisory_failed"] is True and out["status"] == "not_applicable"
        assert "could not start" in out["note"] and "carries on" in out["note"]
        said = _events(c, run_id, "workflow.step_advisory_failed")
        assert len(said) == 1 and "extend_code_task" in said[0]["error"]
        assert not _events(c, run_id, "workflow.step_deadlettered")
        assert not c.execute(text("SELECT 1 FROM deadletters WHERE source_id=:s"),
                             {"s": m["id"]}).first()
        assert steps["j"]["status"] == "pending", "the join still waits for the deploy"

        workflows.complete_step(c, project_id=project, step_run_id=deploy["id"],
                                worker_id=WORKER, output={"deployed": True}, evidence=[])
        after = _claim(c, project, run_id, "after")
        workflows.complete_step(c, project_id=project, step_run_id=after["id"],
                                worker_id=WORKER, output={}, evidence=[])
        steps, done = _steps(c, run_id), _run(c, run_id)

    assert steps["j"]["status"] == "succeeded"
    assert done["status"] == "completed"


def test_an_advisory_step_with_attempts_left_is_retried_not_completed(eng, project):
    with eng.begin() as c:
        run = _pass_two(c, project, advisory_binding=_more_tests(), max_attempts=3)
        m = _claim(c, project, run["id"], "m")
        workflows.fail_step(c, project_id=project, step_run_id=m["id"], worker_id=WORKER,
                            error="a blip", retryable=True)
        steps = _steps(c, run["id"])
        assert steps["m"]["status"] == "ready" and steps["m"]["last_error"] == "a blip"
        assert not _events(c, run["id"], "workflow.step_advisory_failed")
        assert _events(c, run["id"], "workflow.step_retry_scheduled")


def test_the_deploy_failing_beside_an_advisory_step_still_stops_the_run(eng, project):
    """Only the advisory step is tolerated: a deploy that fails is still a stop."""
    with eng.begin() as c:
        run = _pass_two(c, project, advisory_binding=_more_tests())
        _claim(c, project, run["id"], "m")
        d = _claim(c, project, run["id"], "d")
        workflows.fail_step(c, project_id=project, step_run_id=d["id"], worker_id=WORKER,
                            error="the deploy broke", retryable=False)
        steps, stopped = _steps(c, run["id"]), _run(c, run["id"])
    assert steps["d"]["status"] == "deadlettered"
    assert stopped["status"] == "deadlettered"
    assert (stopped["blocked_reason"] or "").startswith("Stopped at")
