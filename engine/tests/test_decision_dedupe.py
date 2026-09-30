"""One open decision per cause, not one per occurrence.

A code task that failed the same way thirty times put thirty identical, unanswerable items in
the decision queue -- "Model capacity exhausted (TPM / rate limit / credits / budget)" x30 and
"Missing active reservation for PR proposal" x14. They are genuinely distinct rows, so no
amount of UI grouping fixes it, and they bury the decisions a person actually has to make.
"""

from __future__ import annotations

import json

from boss_engine.services.core import _create_blocking_decision_postgres as create


class FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeConnection:
    """Records statements; answers the dedupe lookup with whatever is queued."""

    def __init__(self, existing=None):
        self.existing = existing
        self.statements = []

    def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append((text, params or {}))
        if "SELECT id, recommendation FROM decisions" in text:
            return FakeResult(self.existing)
        return FakeResult(None)


def _kinds(connection):
    return [
        "select" if "SELECT id, recommendation" in t
        else "update" if t.strip().upper().startswith("UPDATE") or "UPDATE decisions" in t
        else "insert" if "INSERT INTO decisions" in t
        else "other"
        for t, _ in connection.statements
    ]


def test_the_first_occurrence_creates_the_decision():
    connection = FakeConnection(existing=None)
    out = create(connection, "p1", None, "Model capacity exhausted", {}, {}, "capacity")
    assert out.startswith("dec_")
    assert "insert" in _kinds(connection)


def test_a_repeat_reuses_the_open_decision_instead_of_adding_another():
    connection = FakeConnection(existing={"id": "dec_open", "recommendation": {}})
    out = create(connection, "p1", None, "Model capacity exhausted", {}, {}, "capacity")
    assert out == "dec_open"
    assert "insert" not in _kinds(connection)


def test_a_repeat_counts_the_occurrence_so_the_queue_still_shows_severity():
    connection = FakeConnection(existing={"id": "dec_open", "recommendation": {"occurrences": 4}})
    create(connection, "p1", None, "Model capacity exhausted", {}, {}, "capacity")
    update = [p for t, p in connection.statements if "UPDATE decisions" in t][0]
    assert json.loads(update["recommendation"])["occurrences"] == 5


def test_a_repeat_extends_the_deadline_because_the_cause_is_still_live():
    connection = FakeConnection(existing={"id": "dec_open", "recommendation": {}})
    create(connection, "p1", None, "Model capacity exhausted", {}, {}, "capacity")
    update = [p for t, p in connection.statements if "UPDATE decisions" in t][0]
    assert update["deadline_at"] is not None


def test_the_same_question_about_a_DIFFERENT_task_is_a_different_decision():
    """The lookup is scoped by task_id; two runs blocked the same way are two decisions."""
    connection = FakeConnection(existing=None)
    create(connection, "p1", "tsk_a", "Missing active reservation for PR proposal", {}, {}, "r")
    lookup = [p for t, p in connection.statements if "SELECT id, recommendation" in t][0]
    assert lookup["task_id"] == "tsk_a"


def test_recommendation_stored_as_json_text_is_still_counted():
    """Postgres hands back JSONB as a dict, but a text column would give a string."""
    connection = FakeConnection(existing={"id": "dec_open", "recommendation": '{"occurrences": 2}'})
    create(connection, "p1", None, "Model capacity exhausted", {}, {}, "capacity")
    update = [p for t, p in connection.statements if "UPDATE decisions" in t][0]
    assert json.loads(update["recommendation"])["occurrences"] == 3
