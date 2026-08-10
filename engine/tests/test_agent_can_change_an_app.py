"""Asking the BOSS agent to fix an app must start work, not file a note.

"Anything I ask BOSS agent to fix the existing task, it is unable to trigger anything useful."

It had seven tools. Four read things; the three that wrote anything created TASK ROWS. So
"the server is broken, fix it" resolved, at best, to a to-do item that nothing acts on -- and
the agent had no way to know what apps existed, or what was actually wrong with one, because
it could read repository files but not what BOSS had built.

These pin the three properties that make the request answerable: the agent can find the app,
can determine what is actually wrong with it, and can start real work on it.
"""

from __future__ import annotations

import pytest

from boss_engine.services.agent_loop import TOOLS, WRITE_TOOLS


def tool(name: str) -> dict:
    for entry in TOOLS + WRITE_TOOLS:
        if entry["schema"]["function"]["name"] == name:
            return entry
    raise AssertionError(f"no such agent tool: {name}")


def names() -> set:
    return {e["schema"]["function"]["name"] for e in TOOLS + WRITE_TOOLS}


def test_the_agent_can_find_the_apps_this_project_built():
    """Someone says "the ApproveIt app". Without this there is nothing to attach that to."""
    assert "list_apps" in names()


def test_the_agent_can_determine_what_is_wrong_with_an_app():
    """"It resets when I close the tab" and "the preview is empty" have different causes."""
    schema = tool("inspect_app")["schema"]["function"]
    assert "run_id" in schema["parameters"]["required"]


def test_the_agent_can_start_real_work_on_an_existing_app():
    """The capability whose absence made every such request a dead end."""
    entry = tool("change_existing_app")
    required = entry["schema"]["function"]["parameters"]["required"]
    assert set(required) == {"run_id", "instructions"}
    # A write tool: it spends money and changes a repository, so it carries a risk tier.
    assert entry["risk_tier"] >= 1


def test_the_note_taking_tool_admits_it_does_not_do_anything():
    """The model reached for create_followup_task because it read as the closest match.

    Its description promised "a real follow-up task ... runs immediately", which is true only
    in the sense that a row appears. Tool descriptions are the whole basis for the choice, so
    this one has to say what it does not do.
    """
    description = tool("create_followup_task")["schema"]["function"]["description"]
    assert "change_existing_app" in description
    assert "nothing runs" in description or "no code" in description


def test_the_change_tool_tells_the_agent_when_to_prefer_it():
    """A capability the model never selects is not a capability."""
    description = tool("change_existing_app")["schema"]["function"]["description"]
    assert "create_followup_task" in description
    for phrase in ("already exists", "run_id"):
        assert phrase in description


@pytest.mark.parametrize("name", ["list_apps", "inspect_app", "change_existing_app"])
def test_every_new_tool_is_callable(name):
    assert callable(tool(name)["fn"])
