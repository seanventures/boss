"""Editing a custom step, and seeing what a workflow is made of.

Four gaps reported from the console on 2026-09-24, all the same complaint in different places:
a workflow could be built, saved and started by somebody who could not find out what was in it.
"""
import inspect

import pytest

from boss_engine.services import workflow_custom_steps as steps
from boss_engine.services import workflows


def test_a_custom_step_can_be_changed_at_all():
    """It could be defined and removed and never corrected.

    So fixing a typo in a summary meant deleting the step — and the replacement gets a NEW KEY,
    which every workflow referencing the old one silently loses. Destroying a published
    workflow is a steep price for a wording change.
    """
    assert hasattr(steps, "update"), "there was no way to change a step that exists"


def test_the_key_is_not_something_an_edit_can_change():
    """The key is what a workflow spec binds to.

    A step called "Publish to YouTube" whose key is `publish_to_youtube` has to be renameable to
    "Publish the short" without breaking a single workflow — so the key is an identifier here,
    never a field.
    """
    signature = inspect.signature(steps.update)
    assert "step_key" in signature.parameters
    source = inspect.getsource(steps.update)
    # The key may be MATCHED on, never assigned. Checking the SET clause specifically, because
    # the WHERE clause legitimately mentions it and a naive search finds that instead.
    assigns = source.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "step_key" not in assigns, "an edit must never write a new key"
    assert "WHERE project_id = :p AND step_key = :k" in source


def test_an_edit_cannot_be_used_to_lower_a_risk_tier():
    """The tier is re-resolved from what the step binds, exactly as on create.

    Otherwise "edit the step" is the documented way around the approval floor: set the tier by
    hand and a tier-3 publish stops asking anyone.
    """
    source = inspect.getsource(steps.update)
    assert "risk_tier" not in inspect.signature(steps.update).parameters
    assert "resolve_risk(" in source


def test_a_tier_that_moved_is_recorded_because_nothing_else_would_show_it():
    """Repointing a step at a tier-3 skill changes the approval behaviour of every published
    workflow using it, without any of those workflows being touched. The ledger is the only
    place that connection can be made afterwards."""
    source = inspect.getsource(steps.update)
    assert "risk_tier_before" in source and "risk_tier_after" in source
    assert "workflow.custom_step_updated" in source


def test_switching_binding_clears_the_other_one():
    """A row carrying both a skill and an MCP tool is a row where the next reader has to guess
    which one runs."""
    source = inspect.getsource(steps.update)
    assert "new_server = new_tool = None" in source
    assert "new_skill = None" in source


def test_a_workflow_lists_which_steps_it_has_not_only_how_many():
    """"Steps 4" says how big a workflow is and nothing about what it does.

    Finding out meant opening the editor, which is a strange thing to need before deciding
    whether to start a run.
    """
    assert hasattr(workflows, "_step_outline")
    outline = workflows._step_outline(
        [
            {"id": "fetch-link", "kind": "capability",
             "binding": {"custom_step": "fetch_the_video"}},
            {"id": "publish", "kind": "capability",
             "binding": {"custom_step": "publish_to_youtube"},
             "approval": {"mode": "manual"}},
        ],
        {"fetch_the_video": "Fetch the video", "publish_to_youtube": "Publish to YouTube"},
    )
    assert [s["label"] for s in outline] == ["Fetch the video", "Publish to YouTube"]
    # Which one stops for a person is the other thing worth knowing before starting.
    assert [s["approval"] for s in outline] == ["auto", "manual"]
    assert all(s["mine"] for s in outline)


def test_a_step_whose_definition_was_deleted_still_appears():
    """Falling back to the key beats falling back to nothing: a workflow with a hole in it must
    show the hole, not silently list three steps where it has four."""
    outline = workflows._step_outline(
        [{"id": "gone", "binding": {"custom_step": "deleted_step"}}], {})
    assert outline[0]["label"] == "deleted_step"


@pytest.mark.parametrize("junk", [None, "not a list", 42, [{"no": "binding"}], ["string"]])
def test_a_malformed_spec_does_not_break_the_list(junk):
    """This runs for every workflow on the list screen. A spec shape nobody anticipated must
    not take the page down with it."""
    assert isinstance(workflows._step_outline(junk, {}), list)
