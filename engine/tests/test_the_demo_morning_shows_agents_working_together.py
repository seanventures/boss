"""The demo morning shows BOSS's agents working together on what they noticed (BOSSAPP-904).

Seven signals nobody asked about go through the REAL triage: two alerts on one app become one
incident the Chief of Staff asks about; red CI on a BOSS PR is handed to the engineer, who
proposes a draft fix that the adversarial reviewer agrees with, and it ends resolved; an advisory
is handed to security, who is mid-way; a spend spike and a stale PR wait on a person; a TODO
cluster is only observed. Each item draws as signal → Chief of Staff → owner → outcome. Running
it twice adds nothing; a real project refuses it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import board, demo, ledger, read_model

from ._ambient_support import eng, project  # noqa: F401


@pytest.fixture()
def demo_ok(monkeypatch):
    monkeypatch.setenv("BOSS_AMBIENT_DEMO", "1")


def _by_class(c, pid):
    return {i["task_class"]: i for i in board.list_items(c, pid, limit=50)}


def test_the_morning_runs_every_branch_of_the_flow(eng, project, demo_ok):
    with eng.begin() as c:
        out = demo.load(c, None, project_id=project)
    # In route mode an incident goes straight to the SRE squad (chief_of_staff.ROUTE_AT_ANY_LEVEL):
    # it waits, handed over, for the owners' turn on the tick (owners.work).
    assert out["triage"]["counts"] == {"routed": 3, "proposed": 2, "observed": 1}
    assert sorted(out["owners_played"]) == ["security_reviewer", "software_engineer"]
    with eng.begin() as c:
        items = _by_class(c, project)
        assert {k: i["lifecycle"] for k, i in items.items()} == {
            "incident_response": "proposed", "ci_fix_boss_pr": "resolved",
            "security_patch": "in_progress", "cost_anomaly": "needs_you",
            "stale_handoff": "needs_you", "todo_triage": "proposed"}
        # Two alerts, one incident.
        incident = items["incident_response"]
        assert len([l for l in board.links(c, incident["id"]) if l["kind"] == "signal"]) == 2
        # The CI item: Chief of Staff → engineer, reviewer agrees, resolved — as a diagram.
        g = read_model.graph(c, items["ci_fix_boss_pr"]["id"], project_id=project)
        parties = {n["label"] for n in g["nodes"] if n["type"] == "agent"}
        assert {"Chief of Staff", "Software Engineer", "Adversarial Reviewer"} <= parties
        kinds = [e["kind"] for e in g["edges"]]
        assert {"raised", "handoff", "proposed", "critique", "decided", "resulted"} <= set(kinds)
        assert g["badges"]["outcome"] == "resolved_by_agents"
        decided = [n["label"] for n in g["nodes"] if n["type"] == "decision"]
        assert decided == ["Hand to Software Engineer", "Open draft PR"], "words, not keys"
        deciders = [d["decided_by"] for d in ledger.list_decisions(
            c, project, work_item_id=items["ci_fix_boss_pr"]["id"])]
        assert deciders == ["agent:chief_of_staff", "agent:software_engineer"]
        # The stale PR is the Chief of Staff's own to nudge: it asks "Nudge?", not "Hand to …".
        q = c.execute(text("SELECT question FROM decisions WHERE id = :d"),
            {"d": items["stale_handoff"]["needs_you_decision_id"]}).scalar()
        assert q.startswith("Nudge?")


def test_running_the_morning_again_adds_nothing(eng, project, demo_ok):
    with eng.begin() as c:
        demo.load(c, None, project_id=project)
    with eng.begin() as c:
        before = len(board.list_items(c, project, limit=50))
        again = demo.load(c, None, project_id=project)
        assert again["triage"].get("counts") in (None, {})
        assert len(board.list_items(c, project, limit=50)) == before


def test_a_real_project_refuses_the_demo(eng, project, monkeypatch):
    monkeypatch.delenv("BOSS_AMBIENT_DEMO", raising=False)
    with eng.begin() as c:
        with pytest.raises(PermissionError):
            demo.load(c, None, project_id=project)
