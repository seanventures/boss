"""A workflow step can be told which video to fetch, and the run refuses without it.

"the workflow steps should take the runtime variables like setting the video file url, start
and end clip, icon to add in top corner etc.. How can we supply these inputs to the steps..
today it is not possible."

Half of it already was. `from_input` resolves ANY field name out of the run's input dict, so a
step could always bind `video_url`. What no entry point could do was PASS one — start_workflow
took directive, workflow_key, title and from_discovery, and nothing else — so a workflow that
needed a file to fetch had nowhere to receive it.

TWO SHAPES ALREADY EXIST and this invents no third:

  * {"video_url": "string"}                     — what draft_workflow emits from the wiring
  * {"required": [...], "properties": {...}}    — JSON Schema, the majority of real versions

Only the second distinguishes required from optional, so only it can refuse a run.
"""

from __future__ import annotations

import pytest

from boss_engine.services.workflows import (
    WorkflowValidationError, declared_input_names, missing_required_inputs,
    validate_input_schema,
)

# The workflow from the ask: fetch a video, brand it, publish it.
VIDEO_SCHEMA = {
    "required": ["video_url"],
    "properties": {
        "video_url": {"title": "Video to publish", "type": "string", "format": "uri",
                      "description": "Google Drive share link"},
        "start_clip": {"title": "Start clip", "type": "string"},
        "end_clip": {"title": "End clip", "type": "string"},
    },
}
DRAFTED = {"video_url": "string", "title": "string"}      # the other shape, from draft_workflow


def test_both_shapes_survive_validation_unchanged():
    """A check, not a rewrite: published versions carry these values already."""
    assert validate_input_schema(VIDEO_SCHEMA) == VIDEO_SCHEMA
    assert validate_input_schema(DRAFTED) == DRAFTED


def test_the_names_are_read_from_either_shape():
    assert declared_input_names(VIDEO_SCHEMA) == ["video_url"]
    assert sorted(declared_input_names(DRAFTED)) == ["title", "video_url"]


def test_reading_keys_off_json_schema_does_not_say_required():
    """It once did, and told operators to supply an input called "required"."""
    assert "required" not in declared_input_names(VIDEO_SCHEMA)
    assert "properties" not in declared_input_names(VIDEO_SCHEMA)


def test_a_run_without_the_video_is_refused():
    assert missing_required_inputs({"input_schema": VIDEO_SCHEMA}, {}) == ["video_url"]


def test_a_run_with_it_proceeds():
    supplied = {"video_url": "https://drive.google.com/file/d/abc", "start_clip": "intro.mp4"}
    assert missing_required_inputs({"input_schema": VIDEO_SCHEMA}, supplied) == []


def test_blank_is_not_supplied():
    """An empty box on a form is not an answer."""
    assert missing_required_inputs({"input_schema": VIDEO_SCHEMA}, {"video_url": "   "}) \
        == ["video_url"]


def test_an_optional_input_is_never_demanded():
    supplied = {"video_url": "https://drive.google.com/file/d/abc"}
    assert missing_required_inputs({"input_schema": VIDEO_SCHEMA}, supplied) == []


def test_the_name_to_type_shape_demands_nothing():
    """It says nothing about required, and guessing would refuse runs that were always legal."""
    assert missing_required_inputs({"input_schema": DRAFTED}, {}) == []


def test_a_workflow_declaring_nothing_is_unaffected():
    """Every catalogue workflow today declares none — they must keep starting."""
    assert missing_required_inputs({"input_schema": {}}, {}) == []
    assert missing_required_inputs({}, {}) == []


def test_a_field_name_that_could_not_be_bound_is_rejected():
    """from_input looks the name up verbatim, so a name a step cannot express is a dead input."""
    with pytest.raises(WorkflowValidationError):
        validate_input_schema({"required": ["video url"], "properties": {}})


def test_the_start_tool_lets_a_caller_supply_them():
    """The actual gap: the plumbing resolved any field and nothing could pass one."""
    import inspect
    from boss_engine.tools import builtin

    src = inspect.getsource(builtin)
    assert '"inputs": {' in src
    # Supplied values must not be able to shadow what BOSS itself sets.
    assert 'run_input.update({"directive": directive, "title": title})' in src
