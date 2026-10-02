"""A PRD is titled by what the operator asked for, not by the panel's brief.

`request_meeting` puts the workflow brief FIRST in the panel's directive, so every catalogue
run titled its PRD "PRD: --- WHAT THIS PANEL IS FOR --- You are the panel inside BOSS's…".
The title reads past the brief to the request, and stops before any research block.
"""

from __future__ import annotations

from contextlib import contextmanager

from boss_engine.services import meeting_service as ms
from boss_engine.tools.builtin import _register_write_tools
from boss_engine.tools.registry import ToolRegistry

REQUEST = "Build a team lunch voting app"


def _composed(objective="Ship working, watched software.", research=""):
    """The directive exactly as `request_meeting` builds it, captured at the service."""
    captured: dict = {}

    class _Ctx:
        settings = budget = intake = jobs = None

        @contextmanager
        def begin(self):
            yield None

    def _convene(_connection, **kwargs):
        captured["directive"] = kwargs["directive"]
        return {"run_id": "mrun_x"}

    reg = ToolRegistry()
    _register_write_tools(reg)
    spec = reg.get("request_meeting") if hasattr(reg, "get") else reg._tools["request_meeting"]
    original = ms.convene_prd_meeting_core
    ms.convene_prd_meeting_core = _convene
    try:
        spec.handler(_Ctx(), project_id="p", actor="t",
                     args={"directive": REQUEST, "objective": objective,
                           "workflow_name": "Backend Service Setup", "research": research})
    finally:
        ms.convene_prd_meeting_core = original
    return captured["directive"]


def test_a_directive_that_starts_with_the_panel_brief_is_titled_by_the_request():
    directive = _composed()
    assert directive.startswith(ms.PANEL_BRIEF_MARKER), "the brief no longer comes first"
    assert ms.directive_headline(directive) == REQUEST


def test_research_appended_after_the_request_stays_out_of_the_title():
    directive = _composed(research="Three rivals gate by invite.")
    assert ms.RESEARCH_MARKER in directive
    assert ms.directive_headline(directive) == REQUEST


def test_a_plain_directive_is_its_own_title():
    assert ms.directive_headline("  Build a\n\nthing  ") == "Build a"
    assert ms.directive_headline(f"{REQUEST}\n\n{ms.RESEARCH_MARKER}\nfindings") == REQUEST


def test_a_long_request_is_cut_and_says_so():
    headline = ms.directive_headline("x" * 200, limit=80)
    assert headline == "x" * 80 + "…"


def test_an_empty_request_falls_back_rather_than_titling_with_the_brief():
    brief_only = f"{ms.PANEL_BRIEF_MARKER}\nYou are the panel inside BOSS's workflow."
    assert ms.directive_headline(brief_only) == "Untitled request"
    assert ms.directive_headline("") == "Untitled request"


def test_the_published_prd_and_meeting_are_titled_by_the_request(monkeypatch):
    """The finalize path: the publish decision and the meeting row both carry the request."""
    directive = _composed()
    recorded: dict = {}

    monkeypatch.setattr(ms, "load_meeting_run", lambda _c, _r: {
        "meeting_id": None, "project_id": "p", "task_id": "t", "state": {"prd_markdown": "# x"},
        "role_keys": [], "finalize_ctx": {}, "directive": directive, "url": "",
        "usage": {}, "minutes_artifact_id": None, "decision_id": None,
    })
    monkeypatch.setattr(ms, "resolve_roles", lambda *_a, **_k: [])
    monkeypatch.setattr(ms, "load_role_map", lambda *_a, **_k: {})
    monkeypatch.setattr(ms, "create_artifact", lambda *_a, **_k: "art_1")
    monkeypatch.setattr(ms, "emit_event", lambda *_a, **_k: None)

    def _decision(_c, _p, _t, _title, payload, *_a, **_k):
        recorded["publish_title"] = payload["args"]["title"]
        return "dec_1"

    monkeypatch.setattr(ms, "create_blocking_decision", _decision)

    class _Conn:
        def execute(self, _sql, params=None):
            if params and "title" in params:
                recorded["meeting_title"] = params["title"]

    class _Settings:
        meeting_prd_auto_approve = False

    ms._finalize_meeting(_Conn(), _Settings(), "mrun_x")

    assert recorded["publish_title"] == f"PRD: {REQUEST}"
    assert recorded["meeting_title"] == f"PRD meeting: {REQUEST}"
