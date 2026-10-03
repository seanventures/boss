"""Production launch readiness must wait for the repair it starts.

The step was unreachable, then it was wired into the tool-less branch and returned the raw
`extend_code_task` result. That result is `status: extending`, with the new run id nested
under `result`. The runner only parks a step whose status is `awaiting`, so it recorded the
step succeeded and walked on to the second preview and the deploy gate while a coding agent
was still editing the same branch.

The second preview and the deploy were also still bound to the original build. That adopts
the preview already serving the pre-repair app, then ships that run instead of the repair.
"""

from __future__ import annotations

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_catalogue import CATALOGUE


def _standard() -> dict:
    return next(entry for entry in CATALOGUE if entry["key"] == "standard_product_delivery")


def _step(step_id: str) -> dict:
    return next(step for step in _standard()["steps"] if step["id"] == step_id)


def test_a_readiness_repair_parks_until_the_code_task_finishes(monkeypatch):
    """The shape `extend_code_task` actually returns, through the path the runner takes."""
    monkeypatch.setattr(
        runner, "_plrr_step",
        lambda *args, **kwargs: {
            "capability": "plrr",
            "tool": "extend_code_task",
            "status": "extending",
            "result": {"status": "queued", "run_id": "crun_repair"},
            "note": "continues the existing lineage",
        },
    )

    output = runner.execute_step(
        None, None, project_id="prj", actor="ceo",
        step={"binding": {"capability": "plrr"}},
        context={"run_id": "wfr_1"},
    )

    assert output["status"] == "awaiting"
    assert output["await"] == {"kind": "code_task", "id": "crun_repair"}
    assert output["code_task_run_id"] == "crun_repair"


def test_a_readiness_repair_without_an_id_does_not_count_as_done(monkeypatch):
    monkeypatch.setattr(
        runner, "_plrr_step",
        lambda *args, **kwargs: {
            "capability": "plrr",
            "tool": "extend_code_task",
            "status": "extending",
            "result": {},
        },
    )

    output = runner.execute_step(
        None, None, project_id="prj", actor="ceo",
        step={"binding": {"capability": "plrr"}},
        context={"run_id": "wfr_1"},
    )

    assert output["status"] == "failed"
    assert "no id to wait on" in output["error"]


def test_a_run_with_nothing_built_is_still_not_a_failure(monkeypatch):
    """No build means there is nothing to repair. That is not a failed step."""
    monkeypatch.setattr(
        runner, "_plrr_step",
        lambda *args, **kwargs: {
            "capability": "plrr",
            "status": "not_applicable",
            "note": "no build in this run, so there is nothing to review for readiness.",
        },
    )

    output = runner.execute_step(
        None, None, project_id="prj", actor="ceo",
        step={"binding": {"capability": "plrr"}},
        context={"run_id": "wfr_1"},
    )

    assert output["status"] == "not_applicable"


def test_the_deploy_follows_the_repair_not_the_original_build():
    repair = {"from_step": "plrr", "field": "code_task_run_id"}
    # No second preview after readiness (removed at the operator's request); the deploy is
    # what must follow the repair, and is checked below.
    assert _step("deploy")["binding"]["inputs"]["run_id"] == repair
