"""A custom step stores inputs as objects. The plan must name them, not crash.

GET /v1/delivery-workflows/video_edit_and_publish/plan returned 500 in milliseconds.
The active workflow's custom step declares inputs as
[{name, description, required}, ...]. explain_version used each object as a key
for wired.get(name) and raised TypeError: unhashable type: 'dict'.
"""
from __future__ import annotations

from boss_engine.services.workflow_explain import explain_version

_OBJECT_INPUTS = [
    {"name": "title", "description": "Publication title", "required": True},
    {"name": "channel", "description": "Optional channel", "required": False},
]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Conn:
    """The two reads explain_version makes: the published version, then custom steps."""

    def __init__(self, spec, custom_rows):
        self._version = {
            "definition_id": "wfd_plan",
            "workflow_key": "video_edit_and_publish",
            "name": "Video edit and publish",
            "description": "Cut it and publish it.",
            "is_catalogue": False,
            "active_version_id": "wfv_plan",
            "version_id": "wfv_plan",
            "version": 1,
            "version_status": "published",
            "spec": spec,
        }
        self._custom = custom_rows

    def execute(self, statement, params=None):
        if "workflow_custom_steps" in str(statement):
            return _Result(self._custom)
        return _Result([self._version])


def _custom(step_key, inputs, *, label, summary, produces):
    return {
        "step_key": step_key,
        "label": label,
        "summary": summary,
        "binding_kind": "skill",
        "skill_id": "social_publish",
        "mcp_tool_name": None,
        "inputs": inputs,
        "produces": produces,
    }


def test_explain_version_names_a_custom_steps_object_inputs():
    """Each {name, description, required} object is an input, not a dict key."""
    spec = {
        "entry_step": "publish",
        "steps": [{
            "id": "publish",
            "kind": "capability",
            "next": [],
            "binding": {
                "custom_step": "publish_video",
                "inputs": {"title": {"from_input": "title"}},
            },
        }],
    }
    plan = explain_version(
        _Conn(spec, [_custom(
            "publish_video", _OBJECT_INPUTS,
            label="Publish video", summary="Publishes a video",
            produces=["published_url"],
        )]),
        project_id="proj_test",
        workflow_key="video_edit_and_publish",
    )

    assert plan["published"] is True
    inputs = {item["name"]: item for item in plan["steps"][0]["inputs"]}
    assert set(inputs) == {"title", "channel"}
    assert inputs["title"]["required"] is True
    assert "you supply" in inputs["title"]["source"]
    assert inputs["channel"]["required"] is False
    assert inputs["channel"]["ready"] is True
    assert "optional" in inputs["channel"]["source"]
    assert plan["you_must_supply"] == ["title"]
    assert plan["can_start"] is True
    assert plan["results"] == ["published_url"]


def test_explain_version_explains_a_step_that_declares_no_inputs():
    """An empty input list is a step with nothing to supply, not an error."""
    spec = {
        "steps": [{
            "id": "note",
            "kind": "capability",
            "next": [],
            "binding": {"custom_step": "note_step", "inputs": {}},
        }],
    }
    plan = explain_version(
        _Conn(spec, [_custom(
            "note_step", [],
            label="Note", summary="Writes a note", produces=[],
        )]),
        project_id="proj_test",
        workflow_key="video_edit_and_publish",
    )

    assert plan["published"] is True
    assert [step["id"] for step in plan["steps"]] == ["note"]
    assert plan["steps"][0]["inputs"] == []
    assert plan["steps"][0]["does"] == "Writes a note"
    assert plan["you_must_supply"] == []
    assert plan["can_start"] is True
