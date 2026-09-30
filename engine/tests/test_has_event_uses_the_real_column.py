"""The follow-up guard must ask the events table for a column that exists.

323e5e29 added has_event so a redelivered finalisation would not spawn a second
identical deferred-findings task. Sixteen copies of one follow-up had already
filled the agent's newest-first window and hidden the completed run that
carried the PR.

has_event queried `events.kind`. The table's column is `event_type`. Postgres
raises UndefinedColumn, _already_spawned_followup catches it and returns False,
and every finalize pass spawns another copy. The guard never fired.

Trigger: a code task with deferred review findings is finalised, then finalised
again (lease reclaim, job redelivery). The second pass must see the event the
first pass wrote and refuse to spawn.
"""

from __future__ import annotations


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Conn:
    def __init__(self, *, row=None):
        self.statement = None
        self.params = None
        self._row = row

    def execute(self, stmt, params=None):
        self.statement = stmt
        self.params = params
        return _Result(self._row)


def _sql_text(stmt) -> str:
    return str(getattr(stmt, "text", stmt))


def test_has_event_asks_for_event_type_not_kind():
    """events.kind does not exist. Asking for it makes the guard fail-open."""
    from boss_engine.services.core import has_event

    conn = _Conn()
    has_event(conn, "crun_parent", "code_task.deferred_findings_task")
    sql = _sql_text(conn.statement)
    assert "event_type" in sql, sql
    assert "kind" not in sql.replace("event_type", ""), (
        f"events has event_type, not kind; this SQL will raise UndefinedColumn: {sql}"
    )
    assert conn.params["e"] == "crun_parent"
    assert conn.params["k"] == "code_task.deferred_findings_task"


def test_has_event_is_true_when_the_row_exists():
    from boss_engine.services.core import has_event

    assert has_event(_Conn(row=(1,)), "crun_parent", "code_task.deferred_findings_task") is True
    assert has_event(_Conn(row=None), "crun_parent", "code_task.deferred_findings_task") is False
