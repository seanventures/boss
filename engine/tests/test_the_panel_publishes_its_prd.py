"""A finished meeting must hand its PRD to the step waiting on it.

The meeting capability declares `produces=("meeting_run_id", "prd_markdown", "task_id")`, and
_terminal_evidence — which is what actually writes an awaited step's output — handled
`code_task` and `discovery` and not `meeting`. So a completed panel contributed an id and a
status, and `build`'s binding to meeting.prd_markdown resolved to nothing.

Caught by reading a real run rather than by a test: mrun_b6c8d49458bb47898c60 held 18,979
characters of PRD in meeting_runs.state, and its step output had no prd_markdown key at all.
Binding the input without producing the output looks fixed and changes nothing — which is the
same trap the mapping and the picker fell into on the same day.
"""

from __future__ import annotations

import json

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_capability_runner as runner

PRD = "# PRD: Interview Scorecard App\n\n## 1. Summary\n\nBehavioural anchors per rating.\n"


@pytest.fixture()
def connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE meeting_runs (id TEXT, state TEXT, task_id TEXT)"))
        conn.execute(text("CREATE TABLE discovery_runs (id TEXT, title TEXT, state TEXT)"))
    with engine.begin() as conn:
        yield conn


def _meeting(conn, state: dict, task_id="tsk_1"):
    conn.execute(text("INSERT INTO meeting_runs (id, state, task_id) VALUES ('m1', :s, :t)"),
                 {"s": json.dumps(state), "t": task_id})


def test_the_prd_reaches_the_step_that_was_waiting(connection):
    _meeting(connection, {"prd_markdown": PRD, "round1": {}, "round2": {}})
    evidence = runner._terminal_evidence(connection, "meeting", "m1")
    assert evidence.get("prd_markdown") == PRD.strip()   # surrounding whitespace is trimmed


def test_the_task_and_run_ids_still_travel(connection):
    _meeting(connection, {"prd_markdown": PRD})
    evidence = runner._terminal_evidence(connection, "meeting", "m1")
    assert evidence["meeting_run_id"] == "m1"
    assert evidence["task_id"] == "tsk_1"


def test_a_panel_that_produced_no_prd_says_so_by_omission(connection):
    """Absent, not empty: a binding that resolves to '' would hand the builder a heading and
    an assertion that a spec exists."""
    _meeting(connection, {"round1": {}, "round2": {}})
    assert "prd_markdown" not in runner._terminal_evidence(connection, "meeting", "m1")


def test_a_whitespace_only_prd_is_treated_as_none(connection):
    _meeting(connection, {"prd_markdown": "   \n  "})
    assert "prd_markdown" not in runner._terminal_evidence(connection, "meeting", "m1")


def test_a_real_sized_prd_is_not_truncated(connection):
    """19k and 25k character PRDs are what real panels produce. A requirement dropped by
    truncation is one the build silently will not meet."""
    big = "# PRD\n" + ("Requirement line that must survive.\n" * 900)
    assert len(big) > 19_000
    _meeting(connection, {"prd_markdown": big})
    assert runner._terminal_evidence(connection, "meeting", "m1")["prd_markdown"] == big.strip()


def test_an_unbounded_prd_is_still_bounded(connection):
    _meeting(connection, {"prd_markdown": "x" * (runner._PRD_CHARS + 5_000)})
    got = runner._terminal_evidence(connection, "meeting", "m1")["prd_markdown"]
    assert len(got) == runner._PRD_CHARS


def test_a_missing_meeting_is_not_an_error(connection):
    assert runner._terminal_evidence(connection, "meeting", "gone") == {}
