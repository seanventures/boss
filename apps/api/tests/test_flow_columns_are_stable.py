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
