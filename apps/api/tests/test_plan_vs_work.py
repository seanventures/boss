"""Lines › Plan vs work compares the plan with 30 days of handoffs (org-view spec §4.5, §6.1):
a planned line with no work along it, and a pair that works together (5+ handoffs) with no
line in either direction. `Plan it` proposes the team's manager as the one reported to
(operator decision 2026-10-04), else whoever sends the other more work."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from apps.api.tests import org_board_support as ob


def _line(engine, agent: str, reports_to):
    with engine.begin() as c:
        c.execute(text("INSERT INTO org_assignments (project_id, agent_id, reports_to) "
                       "VALUES (:p, :a, :r)"), {"p": ob.PROJECT, "a": agent, "r": reports_to})


def _handoffs(engine, a: str, b: str, n: int):
    for _ in range(n):
        top = ob.root(engine, started=timedelta(days=2))
        s = ob.act(engine, a, parent=top, root=top, started=timedelta(days=2))
        ob.act(engine, b, parent=s, root=top, started=timedelta(days=2))


def test_a_planned_line_with_no_work_and_unplanned_collaboration_are_both_said(client, engine):
    eng = ob.team("eng")
    helm = ob.role(engine, ob.uid("helm"), eng)
    forge = ob.role(engine, ob.uid("forge"), eng)
    rook = ob.role(engine, ob.uid("rook"), eng)
    quill = ob.role(engine, ob.uid("quill"), eng)
    _line(engine, helm, None)
    _line(engine, rook, helm)        # planned, never worked together → planned_unused
    _line(engine, quill, helm)       # helm is the team's manager (two reports)
    _handoffs(engine, forge, helm, 6)  # works together, no line → unplanned, helm is manager
    _handoffs(engine, forge, rook, 4)  # under 5 → not "works together"
    body = ob.board(client).json()
    drift = body["declared_vs_observed"]
    assert {"agent": rook, "reports_to": helm} in drift["planned_unused"]
    assert {"agent": quill, "reports_to": helm} in drift["planned_unused"]
    pairs = {frozenset((p["a"], p["b"])): p for p in drift["unplanned_pairs"]}
    assert frozenset((forge, rook)) not in pairs
    pair = pairs[frozenset((forge, helm))]
    assert pair["handoffs"] == 6
    assert (pair["parent"], pair["child"], pair["parent_is"]) == (helm, forge, "manager")


def test_a_line_in_either_direction_suppresses_the_pair_and_a_sender_is_the_fallback(client, engine):
    eng = ob.team("eng")
    a = ob.role(engine, ob.uid("a"), eng)
    b = ob.role(engine, ob.uid("b"), eng)
    c = ob.role(engine, ob.uid("c"), eng)
    _line(engine, a, b)
    _handoffs(engine, b, a, 5)   # the line runs a→b; handoffs run b→a: still planned
    _handoffs(engine, c, b, 7)
    _handoffs(engine, b, c, 2)
    drift = ob.board(client).json()["declared_vs_observed"]
    pairs = {frozenset((p["a"], p["b"])): p for p in drift["unplanned_pairs"]}
    assert frozenset((a, b)) not in pairs
    pair = pairs[frozenset((b, c))]
    # No manager in the plan (b has one report… but b is a's manager, so b IS the manager).
    assert pair["parent"] == b and pair["child"] == c
