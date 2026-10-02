"""A meeting is called by what it is about, and its card says where its PRD stands.

The incident (boss-1, 2026-10-02): request_meeting prepends "--- WHAT THIS PANEL IS FOR --- You
are the panel inside BOSS's ... workflow" to the directive, the directive became the title, and
every workflow-convened meeting read "PRD meeting: --- WHAT THIS PANEL IS FOR ---". The four cards
on the Meetings screen and the four consensus options were identical. The cards also said
"awaiting approval" whenever no PR URL was recorded, including meetings whose PRD had fed shipped
projects.
"""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.services import meeting_service as ms

ROOT = Path(__file__).resolve().parents[2]

FRAMED = (
    f"{ms.PANEL_PURPOSE_MARKER}\n"
    "You are the panel inside BOSS's ship-a-web-app workflow. That workflow's objective:\n\n"
    "A shipped app.\n\nWrite for THAT outcome.\n"
    f"{ms.PANEL_ASK_MARKER}\n\nBuild a stock-recommendation site for retail investors"
    f"\n\n{ms.PANEL_RESEARCH_MARKER}\nBOSS researched this before convening you."
)


def test_a_framed_directive_is_called_by_its_ask():
    assert ms.meeting_subject(FRAMED) == "Build a stock-recommendation site for retail investors"


def test_a_stored_title_cut_inside_the_framing_names_its_workflow():
    # boss-1's rows: the title was the framing cut at 80 characters, so the ask never made it.
    stored = "PRD meeting: --- WHAT THIS PANEL IS FOR ---\nYou are the panel inside BOSS's weekly-newsletter workflow. That wo"
    assert ms.display_title(stored) == "PRD meeting: panel for the weekly-newsletter workflow"
    assert "---" not in ms.display_title(stored)


def test_a_plain_directive_keeps_its_words_on_one_line():
    assert ms.meeting_subject("Plan  the\nonboarding  flow") == "Plan the onboarding flow"
    assert ms.meeting_subject("") == "Untitled meeting"
    assert len(ms.meeting_subject("x " * 400)) <= 120


def test_a_non_prd_title_is_not_given_a_prd_prefix():
    assert ms.display_title("[standup] Daily sync") == "[standup] Daily sync"


def test_the_framing_markers_are_written_in_one_place_only():
    # If request_meeting spelled the markers itself, a reworded marker would silently stop
    # meeting_subject from finding the ask, and the titles would be framing again.
    offenders = []
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in (ROOT / base).rglob("*.py"):
            if path.name == "meeting_service.py":
                continue
            if "--- WHAT THIS PANEL IS FOR ---" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_no_meeting_title_is_a_slice_of_the_directive():
    # Every site that names a meeting or its PRD goes through meeting_subject / the run's title.
    pattern = re.compile(r"""(PRD meeting: |"PRD: " \+ |title_prefix\}: )\{?[^"\n]*directive\[:""")
    offenders = []
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in (ROOT / base).rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []


def test_request_meeting_hands_the_bare_ask_over_as_the_title():
    from boss_engine.tools import builtin

    from engine.tests._source import code_only

    src = code_only(Path(builtin.__file__).read_text(encoding="utf-8"))
    body = src[src.index("def request_meeting("):src.index("def start_preview(")]
    assert "title = ms.meeting_subject(directive)" in body
    # Taken BEFORE the framing is wrapped around the directive.
    assert body.index("title = ms.meeting_subject(directive)") < body.index("ms.PANEL_PURPOSE_MARKER")
    assert "title=title," in body


class _Conn:
    """Answers the one decisions lookup prd_state_of makes."""

    def __init__(self, status):
        self.status = status
        self.calls = []

    def execute(self, clause, params):
        self.calls.append(params)
        status = self.status

        class _R:
            def mappings(self):
                return self

            def first(self):
                return None if status is None else {"status": status}

        return _R()


def test_the_prd_state_comes_from_its_decision_not_from_a_pr_url():
    cases = {
        "pending": "awaiting approval",
        "approved": "PRD approved",
        "executed": "PRD published",
        "rejected": "PRD rejected",
        "revision_requested": "revision requested",
    }
    for status, label in cases.items():
        out = ms.prd_state_of(_Conn(status), run_status="completed", decision_id="dec_1",
                              task_id="tsk_1")
        assert out["prd_label"] == label, status
    # A shipped meeting with no PR URL is not "awaiting approval" — the boss-1 cards.
    assert ms.prd_state_of(_Conn("executed"), run_status="completed", decision_id=None,
                           task_id="tsk_1")["prd_state"] == "published"
    assert ms.prd_state_of(_Conn(None), run_status="completed", decision_id=None,
                           task_id=None)["prd_state"] == "no_prd"
    assert ms.prd_state_of(_Conn("pending"), run_status="running", decision_id="d",
                           task_id="t")["prd_label"] == "panel in session"
    assert ms.prd_state_of(_Conn("pending"), run_status="completed", decision_id="d",
                           task_id="t", pull_request_url="https://x/pull/1")["prd_state"] == "pr_open"


def test_every_prd_state_has_operator_words():
    assert set(ms.PRD_STATE_LABELS) >= {
        "running", "failed", "no_prd", "awaiting_approval", "revision_requested",
        "approved", "published", "rejected", "pr_open",
    }
    assert all(label and "_" not in label for label in ms.PRD_STATE_LABELS.values())
