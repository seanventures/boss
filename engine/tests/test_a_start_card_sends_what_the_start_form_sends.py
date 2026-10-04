"""A conversation's start card sends what the composer's start form would.

The card goes through the same door (start_delivery_workflow); what it sends is read from the
workflow's own input_schema the way the console reads it (lib/workflow-inputs.ts): both stored
shapes, a number as a number, a yes/no as a boolean. A card that sent "3" and "false" as text
would start a run the start form never could.
"""
from __future__ import annotations

from boss_engine.services import agent_converse


def test_json_schema_fields_carry_their_kind_and_required():
    fields = agent_converse.input_fields({
        "required": ["video_url", "channel"],
        "properties": {"video_url": {"type": "string", "title": "Video link"},
                       "clips": {"type": "integer"}, "shorts": {"type": "boolean"}},
    })
    assert [(f["name"], f["label"], f["required"], f["kind"]) for f in fields] == [
        ("video_url", "Video link", True, "text"),
        ("clips", "clips", False, "number"),
        ("shorts", "shorts", False, "boolean"),
        # Required with no property is still asked for.
        ("channel", "channel", True, "text"),
    ]


def test_a_name_to_type_map_is_read_too_and_never_required():
    fields = agent_converse.input_fields({"video_url": "string", "clips": "number"})
    assert [(f["name"], f["required"], f["kind"]) for f in fields] == [
        ("video_url", False, "text"), ("clips", False, "number")]


def test_values_are_typed_as_the_start_form_types_them():
    assert agent_converse._typed("number", "3") == 3
    assert agent_converse._typed("number", "2.5") == 2.5
    assert agent_converse._typed("boolean", "false") is False
    assert agent_converse._typed("boolean", "true") is True
    assert agent_converse._typed("text", 42) == "42"
    assert agent_converse._typed("number", "not a number") == "not a number"
