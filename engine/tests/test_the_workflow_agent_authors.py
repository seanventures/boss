"""BOSS can author a workflow in conversation, and cannot author a broken one.

"When I want to create a new workflow, I should be able to discuss with BOSS and create one …
create, edit and delete." The visual builder already existed and the runtime already validated a
spec; what did not exist was a way to describe a sequence and have it become the same object.
"""
import pytest

from boss_engine.agent.engine_agent import EngineAgent
from boss_engine.services.workflows import WorkflowValidationError, validate_workflow_spec
from boss_engine.tools.builtin import (
    _spec_from_steps,
    build_discuss_read_registry,
)


def _steps(*entries):
    return _spec_from_steps(list(entries), {})


def test_the_agent_can_reach_the_authoring_tools():
    names = set(build_discuss_read_registry().names())
    assert {"draft_workflow", "draft_custom_step", "delete_workflow",
            "list_capabilities", "list_skills"} <= names


def test_drafting_is_conversational_and_deleting_is_a_press():
    """A conversation confirmed after every sentence is not a conversation. Deleting something a
    person authored is a different act, and no sentence should make the model press it."""
    assert "delete_workflow" in EngineAgent.DESTRUCTIVE
    assert "draft_workflow" not in EngineAgent.DESTRUCTIVE
    assert "draft_workflow" not in EngineAgent.PROPOSE_ONLY


def test_the_model_describes_a_sequence_and_the_graph_is_built_for_it():
    """Asking a model to emit `next` pointers is asking it to get a graph right in JSON, and the
    failure is silent: a mistyped pointer makes a workflow that stops after step one."""
    spec = _steps({"capability": "build"}, {"capability": "preview"}, {"capability": "notify"})
    ids = [s["id"] for s in spec["steps"]]
    assert ids == ["build", "preview", "notify"]
    assert spec["entry_step"] == "build"
    assert spec["steps"][0]["next"] == ["preview"]
    assert spec["steps"][-1]["next"] == []


def test_it_emits_the_runtime_shape_and_not_a_plausible_one():
    """Every step is kind="capability" with what it runs inside `binding`. Writing
    `kind: "preview"` would be refused — the good outcome; writing a shape the validator accepts
    and the runtime reads differently is the bad one."""
    spec = _steps({"capability": "notify"})
    step = spec["steps"][0]
    assert step["kind"] == "capability"
    assert step["binding"]["capability"] == "notify"


def test_a_custom_step_names_the_step_key_it_runs():
    spec = _steps({"id": "trim", "capability": "custom", "skill": "trim_clip"})
    assert spec["steps"][0]["binding"]["custom_step"] == "trim_clip"


def test_two_steps_of_the_same_kind_both_survive():
    """An operator who asks for two previews should get two, not one silently overwritten."""
    spec = _steps({"capability": "preview"}, {"capability": "preview"})
    assert [s["id"] for s in spec["steps"]] == ["preview", "preview_2"]


def test_the_draft_goes_through_the_same_validator_as_the_builder():
    """A workflow authored in conversation and one drawn by hand are the same object, so they
    are held to the same rules."""
    spec = _steps({"id": "tell", "capability": "notify", "input": {"message": "done"}})
    assert validate_workflow_spec(spec)["steps"][0]["id"] == "tell"


def test_a_step_that_needs_an_input_nothing_produces_is_refused():
    """The most common way a draft fails, and the reason list_capabilities exists."""
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(_steps({"capability": "preview"}))
    assert "run_id" in str(caught.value)


def test_a_workflow_cannot_be_longer_than_a_person_can_read():
    result = _spec_from_steps([{"capability": "notify"}] * 25, {})
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# Phase 3: a step may name its own captain
# --------------------------------------------------------------------------- #
def test_a_step_can_name_its_own_captain():
    """Two steps can run the same capability against different accounts — "publish to our
    channel" and "publish to a client's" are one step kind and two agents."""
    spec = _steps({"id": "tell", "capability": "notify", "input": {"message": "x"},
                   "captain": "release_manager"})
    assert validate_workflow_spec(spec)["steps"][0]["captain"] == "release_manager"


def test_but_not_a_captain_the_workforce_does_not_have():
    """Addressing the dock to an unregistered role is worse than not addressing it: it looks
    like it worked and answers with no lens, no remit and no record."""
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow_spec(_steps({"id": "tell", "capability": "notify",
                                       "input": {"message": "x"}, "captain": "nobody"}))
    assert "not a role this workforce has" in str(caught.value)
