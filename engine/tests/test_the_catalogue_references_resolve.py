"""Every shipped workflow's step references resolve to something that is actually produced.

This is the check that was missing when the `code_task_run_id` bug shipped. The standard
catalogue binds `preview` and `deploy` to {"from_step": "build", "field": "code_task_run_id"};
create_code_task returns `run_id`, and an adopted build calls no tool at all, so that field
existed in the catalogue and in nothing else. Every standard workflow would have failed at
preview — AFTER a successful build, which is the most expensive place to discover it.

Nothing compared the two. A workflow is data, its capabilities are data, and a reference from
one to the other is exactly the kind of thing a test can check statically, for the whole shipped
catalogue at once, in milliseconds. So: no database, no runner, no fixtures.
"""

from __future__ import annotations

import pytest

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import CAPABILITIES
from boss_engine.services.workflow_capabilities import get as get_capability
from boss_engine.services.workflow_catalogue import CATALOGUE


def _steps(workflow: dict) -> list[dict]:
    return list(workflow.get("steps") or [])


def _capability_of(step: dict) -> str | None:
    binding = step.get("binding") or {}
    key = binding.get("capability")
    return str(key) if key else None


ALL_STEPS = [
    pytest.param(wf, step, id=f"{wf['key']}.{step.get('id')}")
    for wf in CATALOGUE
    for step in _steps(wf)
]


@pytest.mark.parametrize("workflow,step", ALL_STEPS)
def test_every_reference_names_a_field_that_step_produces(workflow, step):
    by_id = {s.get("id"): s for s in _steps(workflow)}
    for name, value in ((step.get("binding") or {}).get("inputs") or {}).items():
        if not isinstance(value, dict) or "from_step" not in value:
            continue
        source_id = str(value["from_step"])
        field = str(value.get("field") or "")
        assert source_id in by_id, (
            f"{workflow['key']}.{step['id']} reads from '{source_id}', which is not a step "
            f"in this workflow"
        )
        if not field:
            continue
        source_key = _capability_of(by_id[source_id])
        if source_key is None:
            continue  # a custom or control step declares no produces
        produces = set(get_capability(source_key).produces or ())
        assert field in produces, (
            f"{workflow['key']}.{step['id']} reads '{field}' from '{source_id}', but "
            f"'{source_key}' declares it produces {sorted(produces) or 'nothing'} — this is the "
            f"shape of the bug where preview read code_task_run_id and nothing wrote it"
        )


@pytest.mark.parametrize("workflow,step", ALL_STEPS)
def test_every_required_input_is_bound(workflow, step):
    key = _capability_of(step)
    if key is None:
        return
    assert key in CAPABILITIES, f"{workflow['key']}.{step['id']} names unknown capability '{key}'"
    bound = set(((step.get("binding") or {}).get("inputs") or {}).keys())
    missing = set(get_capability(key).required_inputs or ()) - bound
    assert not missing, (
        f"{workflow['key']}.{step['id']} ('{key}') leaves required input(s) {sorted(missing)} "
        f"unbound — required inputs are deliberately NOT filled from the run input, so this "
        f"workflow fails the moment it reaches this step"
    )


@pytest.mark.parametrize("workflow,step", ALL_STEPS)
def test_a_reference_only_ever_points_backwards(workflow, step):
    """A step cannot read what a later step has not produced yet. The runtime would fail with
    "step X did not produce Y" — true, and useless, when the real error is the ordering."""
    order = {s.get("id"): i for i, s in enumerate(_steps(workflow))}
    here = order[step["id"]]
    for value in ((step.get("binding") or {}).get("inputs") or {}).values():
        if isinstance(value, dict) and "from_step" in value:
            assert order.get(str(value["from_step"]), here) < here, (
                f"{workflow['key']}.{step['id']} reads from '{value['from_step']}', which runs "
                f"after it"
            )


@pytest.mark.parametrize(
    "capability_key", [k for k in CAPABILITIES if k in runner._AWAIT_KINDS]
)
def test_what_the_runtime_writes_is_what_the_capability_claims(capability_key):
    """The declared `produces` is the contract a workflow author binds against. If the runtime
    writes a different name, the contract is a lie and every reference to it fails at run time."""
    kind, _fields = runner._AWAIT_KINDS[capability_key]
    written = runner._CANONICAL_FIELD[kind]
    produces = set(get_capability(capability_key).produces or ())
    assert written in produces, (
        f"'{capability_key}' writes '{written}' but claims to produce {sorted(produces)}"
    )
