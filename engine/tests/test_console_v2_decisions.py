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
