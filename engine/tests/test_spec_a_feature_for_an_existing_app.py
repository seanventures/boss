"""A panel can spec a FEATURE for an app that already exists.

Both build paths start a new app: they seed a scaffold on a new branch. So an approved PRD
could only ever produce a replacement, and the operator's choice was a thought-through rebuild
or an unspecced change — nothing in between. "Convene the panel, then add this to the app I
already have" had nowhere to land.

The marker is an explicit contract between the console and the decision handler, not an
inference about intent: the card appends [boss:extend_run=crun_...] when the panel is about an
existing app, the handler routes the PRD to a follow-up on that app's own branch, and the
marker is stripped before the PRD is used as instructions.
"""

from __future__ import annotations

import re

MARKER = re.compile(r"\[boss:extend_run=(crun_[a-z0-9]+)\]")


def _extract(directive: str) -> tuple[str | None, str]:
    """Mirror of the handler's parse, so the contract is pinned in one obvious place."""
    match = MARKER.search(directive or "")
    if not match:
        return None, directive
    return (
        match.group(1),
        re.sub(r"\s*\[boss:extend_run=crun_[a-z0-9]+\]\s*", " ", directive).strip(),
    )


def test_a_feature_panel_names_the_app_it_extends():
    run_id, _ = _extract('Add a feature to "BugBox" [boss:extend_run=crun_abc123] ...')
    assert run_id == "crun_abc123"


def test_the_marker_never_reaches_the_prd():
    """It is addressed to the pipeline. A PRD containing it would read as noise to the panel
    and, worse, could be echoed into the built app's documentation."""
    _, directive = _extract(
        'Add a feature to "BugBox" — an existing app. [boss:extend_run=crun_abc123]\n\n'
        "What is wanted: bulk close"
    )
    assert "boss:extend_run" not in directive
    assert "bulk close" in directive
    assert "  " not in directive.replace("\n\n", "")   # no double space where it was removed


def test_an_ordinary_directive_is_untouched():
    """A rebuild or a fresh idea must still take the scaffold path."""
    run_id, directive = _extract("Build a new expense tracker with approvals")
    assert run_id is None
    assert directive == "Build a new expense tracker with approvals"


def test_only_a_run_id_shaped_marker_is_honoured():
    """A malformed marker must not silently route work to the wrong place — it should fall
    through to the normal build path instead."""
    assert _extract("[boss:extend_run=tsk_1234] add a thing")[0] is None
    assert _extract("[boss:extend_run=] add a thing")[0] is None


def test_the_handler_and_this_test_share_one_pattern():
    """If the handler's regex drifts from this one, the contract is broken silently — so read
    the real source and compare."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "boss_engine" / "services" / "decisions.py"
    text = source.read_text()
    assert r"\[boss:extend_run=(crun_[a-z0-9]+)\]" in text
    assert "start_followup_task" in text
