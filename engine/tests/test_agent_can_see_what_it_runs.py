"""The agent must be able to look at a preview and a deployment before answering about them.

Asked why a preview showed "Error: HTTP 404", the agent replied that the preview was still
building, that the gateway therefore returned 404, and that the operator should open
`http://127.0.0.1:46181` or wait for a tunnel.

Every part of that was invented. The gateway was returning 200; the page was serving; the
app's own /api calls were 404ing because the preview was publishing built files with no server
behind them; and 127.0.0.1 belongs to a container nobody can reach.

It was not reasoning badly. It had no tool that makes an HTTP request and no tool that reports
what a preview is running, so the question was unanswerable with what it had -- and it answered
anyway. The remedy is eyes, plus tool descriptions that say "do not speculate about this".
"""

from __future__ import annotations

import pytest

from boss_engine.services.agent_loop import TOOLS, WRITE_TOOLS


def tool(name: str) -> dict:
    for entry in TOOLS + WRITE_TOOLS:
        if entry["schema"]["function"]["name"] == name:
            return entry
    raise AssertionError(f"no such agent tool: {name}")


def test_the_agent_can_look_at_a_preview():
    assert tool("diagnose_preview")["schema"]["function"]["parameters"]["required"] \
        == ["preview_id"]


def test_the_agent_can_look_at_a_deployment():
    """A deployment needs no id: "is my app live" should not require one."""
    assert tool("diagnose_deployment")["schema"]["function"]["parameters"]["required"] == []


@pytest.mark.parametrize("name", ["diagnose_preview", "diagnose_deployment"])
def test_the_description_forbids_answering_without_looking(name):
    """The failure was a confident answer, not a missing one. The tool has to say so."""
    description = tool(name)["schema"]["function"]["description"].lower()
    assert "speculate" in description or "do not answer" in description


def test_the_preview_tool_is_pointed_at_the_symptoms_people_actually_report():
    description = tool("diagnose_preview")["schema"]["function"]["description"].lower()
    for symptom in ("404", "blank", "empty", "reload"):
        assert symptom in description, symptom


def test_the_deployment_tool_says_why_a_status_column_is_not_an_answer():
    """`succeeded` means the deploy finished. It does not mean the app works."""
    description = tool("diagnose_deployment")["schema"]["function"]["description"].lower()
    assert "status column" in description
    assert "200" in description


@pytest.mark.parametrize("name", ["diagnose_preview", "diagnose_deployment"])
def test_both_tools_are_callable(name):
    assert callable(tool(name)["fn"])


def test_the_preview_tool_reports_a_missing_preview_rather_than_inventing_one():
    class _Conn:
        def execute(self, *a, **k):
            class _R:
                def mappings(self):
                    return self

                def first(self):
                    return None
            return _R()

    out = tool("diagnose_preview")["fn"](_Conn(), "p", None, {"preview_id": "prev_nope"})
    assert "no such preview" in out["error"]


def test_the_preview_tool_requires_an_id():
    out = tool("diagnose_preview")["fn"](None, "p", None, {})
    assert "required" in out["error"]
