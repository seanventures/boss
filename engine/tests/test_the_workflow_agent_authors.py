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


def _draft(monkeypatch, *, existing, args):
    """Call draft_workflow against a fake connection and a recording workflows module."""
    from contextlib import contextmanager

    from boss_engine.tools import builtin as builtin_mod
    from boss_engine.tools.builtin import build_default_registry

    calls = {"published": [], "mapped": [], "created": []}

    class _Wf:
        @staticmethod
        def publish_version(*_args, **kwargs):
            calls["published"].append(kwargs)
            return {"id": "wfv_new", "version": 2}

        @staticmethod
        def create_definition(*_args, **kwargs):
            calls["created"].append(kwargs)
            return {"id": "wfd_new"}

        @staticmethod
        def create_mapping(*_args, **kwargs):
            calls["mapped"].append(kwargs)
            return {"id": "wfm_new", "selector_value": kwargs.get("selector_value")}

    real_services = builtin_mod.services_module

    def _services(name):
        return _Wf if name == "workflows" else real_services(name)

    monkeypatch.setattr(builtin_mod, "services_module", _services)

    class _Result:
        def scalar(self):
            return None if existing is None else existing.get("id")

        def mappings(self):
            return self

        def first(self):
            return existing

    class _Conn:
        def execute(self, _statement, _params=None):
            return _Result()

    class _Ctx:
        @contextmanager
        def begin(self):
            yield _Conn()

    out = build_default_registry().get("draft_workflow").handler(
        _Ctx(), project_id="proj", actor="ceo", args=args,
    )
    return out, calls


def test_drafting_cannot_overwrite_a_catalogue_workflow(monkeypatch):
    """Standard Product Delivery is read-only. Fork it.

    `draft_workflow` executes without a press. `start_workflow` and the unmapped-project
    default both take the latest published version of `standard_product_delivery`. A model
    that reuses that key — the one `start_workflow` itself names — would silently replace
    the gated path every new Build follows.
    """
    out, calls = _draft(
        monkeypatch,
        existing={"id": "wfd_catalogue", "is_catalogue": True},
        args={"workflow_key": "standard_product_delivery", "name": "Shorter",
              "steps": [{"capability": "notify", "input": {"message": "done"}}]},
    )
    assert out.get("ok") is False
    error = str(out.get("error") or "").lower()
    assert "fork" in error or "catalogue" in error
    assert calls["published"] == []
    assert calls["created"] == []


def test_drafting_cannot_remap_what_build_follows(monkeypatch):
    """Command Center Build posts only work_class=project.new.

    `draft_workflow` is self-executable and its schema names project.new as the example.
    create_mapping at priority 100 is the same slot Apply uses, and resolve_mapping
    prefers the highest priority. A conversation that drafts a shorter path would
    silently become the next Build — research, panel, merge and deploy never run.
    """
    out, calls = _draft(
        monkeypatch,
        existing=None,
        args={"workflow_key": "simple_ship", "name": "Simple ship",
              "work_class": "project.new",
              "steps": [{"capability": "notify", "input": {"message": "done"}}]},
    )
    assert out.get("status") == "published"
    assert calls["published"], "the new workflow should still be authored"
    assert calls["mapped"] == []
    note = str(out.get("mapping") or out.get("note") or out.get("error") or "").lower()
    assert "not mapped" in note or "apply" in note


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


def test_a_declared_input_reaches_the_start_form():
    """`inputs` decided the wiring and was then thrown away.

    A published version carried `input_schema: {}` while its own steps referenced
    `from_input`, so the two values an operator had to supply were recorded nowhere. The
    bindings still resolved, which is why this survived: the workflow was startable by any
    caller that already knew the field names, and unstartable from the console's form, which
    renders FROM the schema and had nothing to draw.

    Found by the Program Manager agent reading its published spec back out of the database
    instead of trusting draft_workflow's success reply.
    """
    spec = _spec_from_steps(
        [{"capability": "video_edit"}, {"capability": "social_publish"}],
        {"inputs": ["media_artifact_id", "title"]},
    )
    # Declared, so the form has something to render.
    assert "media_artifact_id" in spec["input_schema"]
    assert "title" in spec["input_schema"]
    # And it survives the validator, which used to default the field to {}.
    assert validate_workflow_spec(spec)["input_schema"]["title"] == "string"


def test_an_input_no_step_consumes_is_not_put_on_the_form():
    """The schema comes from what the wiring BOUND, not from what the model listed.

    A model that lists a spare input should not produce a form field for a value the run
    will ignore — the operator would fill it in and wonder why nothing used it.
    """
    spec = _spec_from_steps(
        [{"capability": "video_edit"}, {"capability": "social_publish"}],
        {"inputs": ["media_artifact_id", "title", "unused_extra"]},
    )
    assert "unused_extra" not in spec.get("input_schema", {})


def test_a_workflow_that_needs_nothing_declares_nothing():
    """`media_artifact_id` comes from the step above it, so it is not the operator's to give."""
    spec = _spec_from_steps([{"capability": "notify"}], {})
    assert "input_schema" not in spec or spec["input_schema"] == {}
