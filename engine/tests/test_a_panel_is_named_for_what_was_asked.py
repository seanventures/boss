"""A panel is named for what the operator asked, not for the prompt scaffolding around it.

On boss-1 the task tsk_c797c430… was titled "PRD meeting: --- WHAT THIS PANEL IS FOR --- You are
the pane…": request_meeting put a purpose block FIRST in the directive (so every seat reads it
before the ask), and the task title was the directive's first 90 characters. The Command
Center, the Factory's panel cards and the Approvals evidence chip all showed that.
"""
from __future__ import annotations

import inspect

from boss_engine.services import meeting_service as ms
from boss_engine.tools import builtin


def wrapped(ask: str, *, research: str = "") -> str:
    text = (f"{ms.PANEL_PURPOSE_MARK}\nYou are the panel inside BOSS's Standard workflow. "
            f"That workflow's objective:\n\nShip it.\n\n{ms.PANEL_ASK_MARK}\n\n{ask}")
    if research:
        text += f"\n\n{ms.PANEL_RESEARCH_MARK}\nBOSS researched this.\n\n{research}"
    return text


def test_the_subject_is_the_ask_inside_the_scaffolding():
    assert ms.directive_subject(wrapped("Build an incident timeline builder.")) == \
        "Build an incident timeline builder."


def test_research_after_the_ask_is_not_part_of_its_name():
    assert ms.directive_subject(wrapped("Build a bug box.", research="Five competitors…")) == \
        "Build a bug box."
    assert ms.directive_subject(f"Build a bug box.\n\n{ms.PANEL_RESEARCH_MARK}\nfindings") == \
        "Build a bug box."


def test_a_plain_directive_is_its_own_subject():
    assert ms.directive_subject("  Build a\nbug box. ") == "Build a bug box."


def test_request_meeting_builds_the_directive_from_the_same_markers():
    """One definition, so the stripper cannot fall out of step with the builder."""
    source = inspect.getsource(builtin)
    at = source.index("def request_meeting(")
    body = source[at:source.index("@reg.tool", at)]
    # Integration: one definition of each marker (meeting_service.PANEL_*_MARKER, which the
    # console-b stream's meeting_subject reads); the *_MARK names are the same objects.
    for mark in ("PANEL_PURPOSE", "PANEL_ASK", "PANEL_RESEARCH"):
        assert f"ms.{mark}_MARKER" in body
        assert getattr(ms, f"{mark}_MARK") is getattr(ms, f"{mark}_MARKER")
    assert "--- WHAT THIS PANEL IS FOR ---" not in body


def test_the_task_title_uses_the_subject():
    source = inspect.getsource(ms.convene_prd_meeting_core)
    # Integration: the console-b stream's subject (meeting_subject — the caller's bare ask, or
    # the directive with its framing stripped and bounded) titles the task; the console-a
    # stream's directive_subject slice was the same fix for the same incident.
    assert "title = meeting_subject(title or directive)" in source
    assert 'title=f"PRD meeting: {title}"' in source
