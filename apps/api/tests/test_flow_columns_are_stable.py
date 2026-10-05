"""Flow's columns come from where each team usually joins a run (org-view spec §4.3): the same
data gives the same columns on every read, and a team with no runs has none."""
from __future__ import annotations

from datetime import timedelta

from apps.api.tests import org_board_support as ob


def test_columns_follow_where_teams_join_and_do_not_move(client, engine):
    lead, eng, qa, idle = ob.team("lead"), ob.team("eng"), ob.team("qa"), ob.team("idle")
    helm = ob.role(engine, ob.uid("helm"), lead)
    forge = ob.role(engine, ob.uid("forge"), eng)
    rook = ob.role(engine, ob.uid("rook"), qa)
    ob.role(engine, ob.uid("sleepy"), idle)
    for i in range(3):
        top = ob.root(engine, started=timedelta(hours=10 - i))
        a = ob.act(engine, helm, parent=top, root=top, started=timedelta(hours=9 - i, minutes=50))
        b = ob.act(engine, forge, parent=a, root=top, started=timedelta(hours=9 - i, minutes=40))
        ob.act(engine, rook, parent=b, root=top, started=timedelta(hours=9 - i, minutes=30))
    first = ob.board(client).json()
    cols = {t["key"]: t["flow_col"] for t in first["teams"]}
    assert cols[lead] < cols[eng] < cols[qa]
    assert cols[idle] is None
    again = ob.board(client).json()
    assert {t["key"]: t["flow_col"] for t in again["teams"]} == cols


def test_no_edge_has_both_ends_in_one_column(client, engine):
    """The walk's shape (D2): Architecture and Engineering usually join runs at the same point,
    so by join order alone they shared a column and the heaviest edge between them was drawn
    under both nodes. Layered along the edges, every edge spans at least one column."""
    lead, arch, eng = ob.team("lead"), ob.team("arch"), ob.team("eng")
    helm = ob.role(engine, ob.uid("helm"), lead)
    atlas = ob.role(engine, ob.uid("atlas"), arch)
    forge = ob.role(engine, ob.uid("forge"), eng)
    for i in range(3):  # lead → arch → eng
        top = ob.root(engine, started=timedelta(hours=10 - i))
        a = ob.act(engine, helm, parent=top, root=top, started=timedelta(hours=9 - i, minutes=50))
        b = ob.act(engine, atlas, parent=a, root=top, started=timedelta(hours=9 - i, minutes=40))
        ob.act(engine, forge, parent=b, root=top, started=timedelta(hours=9 - i, minutes=30))
    for i in range(4):  # lead → eng directly
        top = ob.root(engine, started=timedelta(hours=5 - i))
        a = ob.act(engine, helm, parent=top, root=top, started=timedelta(hours=4 - i, minutes=50))
        ob.act(engine, forge, parent=a, root=top, started=timedelta(hours=4 - i, minutes=40))
    body = ob.board(client).json()
    cols = {t["key"]: t["flow_col"] for t in body["teams"]}
    assert cols[lead] < cols[arch] < cols[eng], cols
    for e in body["team_edges"]:
        if e["from"] == "person" or cols.get(e["from"]) is None or cols.get(e["to"]) is None:
            continue
        assert cols[e["from"]] != cols[e["to"]], (e, cols)


def test_layering_points_every_edge_across_columns_even_against_the_flow():
    from boss_engine.services.agents_org_board import layer

    key = {"a": (0, 0, "a"), "b": (1, 1, "b"), "c": (1, 2, "c"), "d": (2, 3, "d")}
    # b and c tie on join order; c → b runs against TEAM_ORDER; d → a is a loop back.
    cols = layer(key, [("a", "b"), ("c", "b"), ("b", "d"), ("d", "a"), ("a", "c")])
    for x, y in [("a", "b"), ("c", "b"), ("b", "d"), ("d", "a"), ("a", "c")]:
        assert cols[x] != cols[y], (x, y, cols)
