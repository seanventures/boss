"""The v2 decision queue.

The parity map flags this area's risk as "a gate that silently kills work", and that phrase is
literal: a decision carries a deadline_at and a default_action, so an unanswered gate does not
wait — it resolves itself, and the work either proceeds unreviewed or is dropped.

A queue that shows the question but not that consequence reads as a list of things you could
get to later, when it is actually a list of things about to be decided for you.
"""

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services.console_v2 import decision_queue

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, statement, params=None):
        # RAW, newlines intact. Normalising here would join a `--` comment onto the line
        # below it, so stripping comments afterwards eats the rest of the query.
        self.statements.append((str(statement), dict(params or {})))
        conn = self

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in conn.rows]

        return _R()


def gate(did, *, question="Approve the PRD?", status="pending", deadline_min=None,
         default_action=None, risk=1, options=None, rec=None, delegated=None, age_min=0):
    return {
        "id": did,
        "task_id": "task_1",
        "question": question,
        "recommendation": rec or {},
        "options": options if options is not None else [],
        "risk_tier": risk,
        "cost_summary": {},
        "deadline_at": NOW + timedelta(minutes=deadline_min) if deadline_min is not None else None,
        "default_action": default_action,
        "status": status,
        "delegated_to": delegated,
        "created_at": NOW - timedelta(minutes=age_min),
    }


# --------------------------------------------------------------------------- #
# What happens if nobody answers
# --------------------------------------------------------------------------- #
def test_a_gate_that_will_resolve_itself_says_so():
    """This is the whole point of the screen. "approve" plus a deadline means the work
    proceeds unreviewed unless someone intervenes."""
    out = decision_queue(FakeConn([gate("d1", deadline_min=120, default_action="approve")]), "p")
    assert out["items"][0]["if_ignored"] == "approve automatically"


def test_a_gate_that_waits_forever_says_that_instead():
    """"waits for you" and "proceeds in two hours" are opposite facts. Rendering both as a
    pending row makes the second invisible."""
    out = decision_queue(FakeConn([gate("d1")]), "p")
    assert "nothing happens on its own" in out["items"][0]["if_ignored"]


def test_a_default_with_no_deadline_is_described_honestly():
    """It will fire — we just cannot say when."""
    out = decision_queue(FakeConn([gate("d1", default_action="reject")]), "p")
    assert out["items"][0]["if_ignored"] == "reject when it expires"


def test_the_self_resolving_ones_are_counted_separately():
    """A different kind of urgent: these get decided without you."""
    out = decision_queue(FakeConn([
        gate("d1", deadline_min=30, default_action="approve"),
        gate("d2"),
        gate("d3", deadline_min=90, default_action="reject"),
    ]), "p")
    assert out["total"] == 3
    assert out["auto_resolving"] == 2


# --------------------------------------------------------------------------- #
# Order: the ones about to fire come first
# --------------------------------------------------------------------------- #
def test_the_query_orders_by_deadline_with_undated_last():
    """A gate with no deadline must never outrank one that is about to fire on its own."""
    import re as _re

    conn = FakeConn([gate("d1")])
    decision_queue(conn, "p")
    # Strip SQL comments before looking: asserting on the raw text would be asserting on where
    # the explanation happens to sit, not on the ordering.
    sql_text = _re.sub(r"--[^\n]*", " ", conn.statements[0][0]).lower()
    sql_text = " ".join(sql_text.split())
    order = sql_text[sql_text.index("order by"):]
    assert "deadline_at asc nulls last" in order
    # And it must be the FIRST key, or a high-risk gate with no deadline would jump the queue
    # ahead of one that fires in ten minutes.
    assert order.index("deadline_at") < order.index("risk_tier") < order.index("created_at")


def test_only_open_gates_are_queued():
    """An answered decision is not waiting on anyone."""
    conn = FakeConn([gate("d1")])
    decision_queue(conn, "p")
    assert set(conn.statements[0][1]["open"]) == {"pending", "delegated", "revision_requested"}


def test_a_delegated_gate_is_still_open_and_says_who_has_it():
    """Delegated is not resolved. Dropping it from the queue is how a gate goes quiet."""
    out = decision_queue(FakeConn([gate("d1", status="delegated", delegated="ceo")]), "p")
    assert out["items"][0]["status"] == "delegated"
    assert out["items"][0]["delegated_to"] == "ceo"


# --------------------------------------------------------------------------- #
# Enough to decide without leaving the page
# --------------------------------------------------------------------------- #
def test_the_question_and_the_options_travel_together():
    """A gate with no options rendered is a question with no way to answer it."""
    out = decision_queue(FakeConn([gate(
        "d1", question="Ship Shorty to staging?",
        options=[{"id": "yes", "label": "Approve and deploy"}, {"id": "no", "label": "Reject"}],
    )]), "p")
    item = out["items"][0]
    assert item["question"] == "Ship Shorty to staging?"
    assert item["options"] == ["Approve and deploy", "Reject"]


def test_plain_string_options_are_handled():
    out = decision_queue(FakeConn([gate("d1", options=["approve", "reject"])]), "p")
    assert out["items"][0]["options"] == ["approve", "reject"]


def test_the_recommendation_is_carried_when_there_is_one():
    out = decision_queue(FakeConn([gate("d1", rec={"summary": "Approve — gates passed"})]), "p")
    assert out["items"][0]["recommendation"] == "Approve — gates passed"


def test_a_missing_recommendation_is_empty_not_invented():
    """No recommendation and a bland one look the same on screen; only one is honest."""
    out = decision_queue(FakeConn([gate("d1")]), "p")
    assert out["items"][0]["recommendation"] == ""


def test_the_whole_queue_is_one_query():
    conn = FakeConn([gate("d1"), gate("d2")])
    decision_queue(conn, "p")
    assert len(conn.statements) == 1


def test_an_empty_queue_is_reported_as_empty_not_as_an_error():
    out = decision_queue(FakeConn([]), "p")
    assert out["items"] == []
    assert out["total"] == 0
    assert out["auto_resolving"] == 0


# --------------------------------------------------------------------------- #
# Panels
#
# The reference streams a scripted debate — agents speaking in turn, votes tallying, a verdict.
# BOSS's panels are real and produce real minutes, but they are NOT streamed. Animating a
# debate that nobody is watching would be the most convincing lie in the console.
# --------------------------------------------------------------------------- #
from boss_engine.services.console_v2 import panel_sessions  # noqa: E402


def meeting(mid, *, title="Pricing panel", status="completed", participants=None,
            decisions=None, dissent=None, blocked=None, minutes="art_1", age_min=0):
    return {
        "id": mid,
        "title": title,
        "purpose": "Decide the pricing model.",
        "status": status,
        "participants": participants if participants is not None else [{"role": "cpo"}, {"role": "cto"}],
        "decisions": decisions if decisions is not None else [{"summary": "Go freemium"}],
        "dissent_records": dissent or [],
        "minutes_artifact_id": minutes,
        "transcript_artifact_id": None,
        "blocked_state": blocked,
        "completed_at": NOW - timedelta(minutes=age_min),
        "created_at": NOW - timedelta(minutes=age_min + 5),
    }


def test_the_payload_says_panels_are_not_streamed():
    """So the screen cannot animate a debate that is not being watched."""
    out = panel_sessions(FakeConn([meeting("m1")]), "p")
    assert out["streamed"] is False
    assert "not streamed" in out["stream_note"]


def test_a_running_panel_is_marked_deliberating():
    out = panel_sessions(FakeConn([meeting("m1", status="running")]), "p")
    assert out["sessions"][0]["deliberating"] is True
    assert out["deliberating"] == 1


def test_a_completed_panel_is_not_deliberating():
    out = panel_sessions(FakeConn([meeting("m1", status="completed")]), "p")
    assert out["sessions"][0]["deliberating"] is False


def test_dissent_is_carried_separately_from_the_decision():
    """A panel that agreed and a panel that overruled an objection produce the same decision
    and are not the same event. BOSS records this and v1 never showed it."""
    out = panel_sessions(FakeConn([meeting(
        "m1",
        decisions=[{"summary": "Ship it"}],
        dissent=[{"note": "Legal objected: consent copy unresolved"}],
    )]), "p")
    session = out["sessions"][0]
    assert session["decisions"] == ["Ship it"]
    assert "Legal objected" in session["dissent"][0]


def test_a_blocked_panel_is_neither_running_nor_concluded():
    """Showing it as either loses the fact that it needs someone."""
    out = panel_sessions(FakeConn([meeting(
        "m1", status="blocked", blocked={"reason": "waiting on the PRD"},
    )]), "p")
    session = out["sessions"][0]
    assert session["blocked"] is True
    assert "waiting on the PRD" in session["blocked_reason"]
    assert out["blocked"] == 1


def test_a_panel_with_no_minutes_says_so():
    out = panel_sessions(FakeConn([meeting("m1", minutes=None)]), "p")
    assert out["sessions"][0]["has_minutes"] is False


def test_panelists_are_listed():
    out = panel_sessions(FakeConn([meeting("m1", participants=[{"role": "cpo"}, "cto"])]), "p")
    assert out["sessions"][0]["panelists"] == ["cpo", "cto"]


def test_the_title_is_cleaned_like_everywhere_else():
    out = panel_sessions(FakeConn([meeting("m1", title="Shorty — Re-build PRD")]), "p")
    assert out["sessions"][0]["title"] == "Shorty"
