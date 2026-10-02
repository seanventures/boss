"""A role placed under a parent the plan does not contain brings that parent into the plan.

Team structure's "Reports to" offered roles from the observed tree when nothing was planned,
and set_one never checked the parent was planned. The child was saved reporting to a role the
chart did not contain, so it rendered at the top level with "Reports to: <parent>" beside it.
"""
from __future__ import annotations

from typing import Any

import pytest

from boss_engine.services import org_assignments as oa


class FakeConnection:
    """Records org_assignments writes; enough of a Connection for set_one."""

    def __init__(self, rows: dict[str, dict[str, Any]]):
        self.rows = rows
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if sql.lstrip().startswith("INSERT INTO org_assignments"):
            self.rows.setdefault(params["a"], {"reports_to": params.get("r")})
        return self


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(oa, "emit_event", lambda *a, **k: None)


def use(monkeypatch, rows):
    monkeypatch.setattr(oa, "get_all", lambda _c, _p: {k: dict(v) for k, v in rows.items()})
    return FakeConnection(rows)


def test_an_unplanned_parent_is_placed_at_the_top(monkeypatch):
    connection = use(monkeypatch, {})
    oa.set_one(connection, "p", "qa", reports_to="cto", actor="ceo")
    assert set(connection.rows) == {"cto", "qa"}
    assert connection.rows["cto"]["reports_to"] is None


def test_a_parent_already_in_the_plan_is_left_where_it_is(monkeypatch):
    connection = use(monkeypatch, {"cto": {"reports_to": "ceo_role"}, "ceo_role": {"reports_to": None}})
    oa.set_one(connection, "p", "qa", reports_to="cto", actor="ceo")
    inserts = [s for s in connection.sql if "INSERT INTO org_assignments" in s]
    assert len(inserts) == 1  # only qa's own row


def test_a_cycle_is_still_refused(monkeypatch):
    connection = use(monkeypatch, {"qa": {"reports_to": None}, "cto": {"reports_to": "qa"}})
    with pytest.raises(oa.OrgCycle):
        oa.set_one(connection, "p", "qa", reports_to="cto", actor="ceo")
