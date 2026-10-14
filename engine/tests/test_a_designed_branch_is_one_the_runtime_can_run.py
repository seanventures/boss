"""A branch drawn in the workflow designer is one the runtime can run.

The console's designer places Branch, Do at the same time and Repeat until, and builds a
condition from parts. apps/web/tests/workflow-blocks.test.ts writes every spec its own edits make
to apps/web/tests/fixtures/designer-control-flow.json (and refuses a stale file); this runs each
one through the validator Check and Save use, and the evaluator a run uses.

It also pins the two rules the designer leans on:

* A condition must say what it checks. `{}` — a Branch nobody described — used to publish and then
  stop the run when it was reached. `expression_problems` reads an expression the way
  `evaluate_expression` does, and refuses what it would refuse.
* Each path of a condition starts with a step of its own. A condition skips the first step of the
  path it does not take, and a skipped step is never started again, so a "no" path pointed at the
  step the "yes" path also reaches would skip that step on "yes". Check says so for the operator's
  own workflows (`condition_path_problems`).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from boss_engine.services.workflows import (
    EXPRESSION_OPS,
    WorkflowValidationError,
    binding_order_problems,
    condition_path_problems,
    evaluate_expression,
    expression_problems,
    normalise_workflow_spec,
    validate_workflow_spec,
)

FIXTURE = (Path(__file__).resolve().parents[2]
           / "apps" / "web" / "tests" / "fixtures" / "designer-control-flow.json")


def _designed() -> dict[str, dict]:
    return json.loads(FIXTURE.read_text())["specs"]


@pytest.mark.parametrize("name", sorted(_designed()))
def test_every_designed_spec_passes_the_validator_check_and_save_use(name):
    spec = _designed()[name]
    normalized, problems = normalise_workflow_spec(spec, check_agents=False,
                                                   require_gate_question=True)
    problems += binding_order_problems(normalized, lambda step: frozenset())
    problems += condition_path_problems(normalized)
    assert problems == [], problems
    assert validate_workflow_spec(spec, check_agents=False)["entry_step"] == spec["entry_step"]


def test_the_designed_conditions_mean_what_the_builder_said():
    specs = _designed()
    branch = next(s for s in specs["branch"]["steps"] if s["id"] == "branch")["expression"]
    # "any of: the instructions contain 'urgent', the draft's risk is at least 3"
    assert evaluate_expression(branch, {"input": {"instructions": "an URGENT fix"}, "steps": {}})
    assert evaluate_expression(branch, {"input": {"instructions": "tidy"},
                                        "steps": {"draft": {"output": {"risk": 4}}}})
    assert not evaluate_expression(branch, {"input": {"instructions": "tidy"},
                                            "steps": {"draft": {"output": {"risk": 1}}}})
    loop = next(s for s in specs["loop"]["steps"] if s["id"] == "repeat")["expression"]
    # Repeat UNTIL approved: it keeps going (true) until the latest round says approved.
    assert evaluate_expression(loop, {"context": {}})
    assert evaluate_expression(loop, {"context": {"approved": False}})
    assert not evaluate_expression(loop, {"context": {"approved": True}})


def test_an_empty_condition_does_not_publish():
    spec = {"entry_step": "c", "steps": [
        {"id": "c", "kind": "condition", "expression": {}, "on_true": "a", "on_false": "b"},
        {"id": "a", "kind": "task"}, {"id": "b", "kind": "task"}]}
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(spec, check_agents=False)
    assert [(p["step_id"], p["field"]) for p in caught.value.problems] == [("c", "expression")]
    assert "does not say what to check" in caught.value.problems[0]["message"]


@pytest.mark.parametrize("expression, said, at_run_time", [
    ({}, "does not say what to check", "raises"),
    ({"path": "  "}, "does not say what to check", "raises"),
    ({"path": "input.x", "op": "equals"}, "not a comparison BOSS knows", "raises"),
    ({"any": "x"}, "at least one comparison", "raises"),
    ({"not": {"all": [{"path": "input.x", "op": "nope"}]}}, "not a comparison BOSS knows", "raises"),
    # These never raise: they are silently never true, which is worse.
    ({"path": "input.x", "op": "in", "value": "a"}, "list of values", "never true"),
    ({"path": "input.x", "op": "gt"}, "needs a value", "never true"),
    ({"any": []}, "at least one comparison", "never true"),
])
def test_expression_problems_refuses_what_the_evaluator_cannot_use(expression, said, at_run_time):
    found = expression_problems(expression)
    assert len(found) == 1 and said in found[0], found
    if at_run_time == "raises":
        with pytest.raises(WorkflowValidationError):
            evaluate_expression(expression, {"input": {"x": 1}})
    else:
        assert evaluate_expression(expression, {"input": {"x": 1}}) is False


def test_an_expression_that_is_not_an_object_is_refused():
    assert expression_problems([]) == ["the condition must be an object."]


@pytest.mark.parametrize("op", EXPRESSION_OPS)
def test_every_comparison_the_evaluator_knows_is_accepted(op):
    value = ["a"] if op in {"in", "not_in"} else 1
    expression = {"path": "input.x", "op": op, "value": value}
    assert expression_problems(expression) == []
    evaluate_expression(expression, {"input": {"x": 1}})


def test_keys_the_evaluator_does_not_read_are_left_alone():
    # Written by hand before the builder; `op` defaults to truthy, `equals` is ignored.
    assert expression_problems({"path": "input.urgent", "equals": True}) == []


def _condition(on_true: str, on_false: str, extra: list[dict]) -> dict:
    return {"entry_step": "c", "steps": [
        {"id": "c", "kind": "condition", "expression": {"path": "input.x"},
         "on_true": on_true, "on_false": on_false}, *extra]}


def test_a_path_starting_where_the_other_path_goes_is_named():
    spec = _condition("hotfix", "deploy", [
        {"id": "hotfix", "kind": "task", "next": ["deploy"]}, {"id": "deploy", "kind": "task"}])
    found = condition_path_problems(spec)
    assert [(p["step_id"], p["field"]) for p in found] == [("c", "on_false")]
    assert "would never run" in found[0]["message"]
    flipped = _condition("deploy", "hotfix", spec["steps"][1:])
    assert [p["field"] for p in condition_path_problems(flipped)] == ["on_true"]
    both = _condition("deploy", "deploy", spec["steps"][1:])
    assert [p["field"] for p in condition_path_problems(both)] == ["on_false"]


def test_paths_that_meet_after_their_own_first_steps_are_fine():
    spec = _condition("hotfix", "plan", [
        {"id": "hotfix", "kind": "task", "next": ["deploy"]},
        {"id": "plan", "kind": "task", "next": ["deploy"]},
        {"id": "deploy", "kind": "task"}])
    assert condition_path_problems(spec) == []
