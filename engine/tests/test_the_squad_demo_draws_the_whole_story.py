"""The squad demo draws the whole story as one diagram (BOSSAPP-908; design §11.0, §13.3).

The non-production demo — a CVE squad of four with a critique, a panel, a dissent, a build and
a merge that only a person may decide — runs through the real squad machinery with a scripted
model and stand-in launchers, and the Agent Operations graph shows it without a paragraph:
signal → Chief of Staff → squad and seats → proposals with critiques and votes (dissent as split
arrows) → decisions → the panel and the run, handed off and reporting back → Needs you. When a
person approves, their decision joins the diagram and the lead resolves the item.
"""
from __future__ import annotations

import pytest

from boss_engine.services.ambient import read_model, squad_demo, squad_turns

from ._ambient_support import eng, project  # noqa: F401
from ._squad_support import LEAD, ScriptedSquadModel, move, settings


def test_the_demo_refuses_without_saying_it_is_a_demo(eng, project):
    with pytest.raises(PermissionError, match="confirm_demo"):
        squad_demo.seed(eng, settings(), project_id=project)


def test_the_demo_draws_signal_squad_critique_panel_dissent_run_and_needs_you(eng, project):
    out = squad_demo.seed(eng, settings(), project_id=project, confirm_demo=True,
                          ensure_roles=True)
    g, run = out["graph"], out["run"]
    types = [n["type"] for n in g["nodes"]]
    labels = {n["label"] for n in g["nodes"]}
    kinds = [e["kind"] for e in g["edges"]]
    dissent = [e for e in g["edges"] if e["dissent"]]

    assert run["stopped"]["guard"] == "needs_you" and run["rounds"] == 5
    assert [s["state"] for s in run["settled"]] == ["revise", "decided", "decided", "needs_you"]
    assert g["badges"]["needs_you"] and g["badges"]["dissent"]
    assert g["badges"]["problem_type"] == "Security patch"
    assert types.count("agent") == 5 and "squad" in types and "needs_you" in types
    assert {"CVE squad", "Convene a panel", "Open draft PR", "Merge", "Panel", "Run",
            "Revise"} <= labels
    assert {"raised", "assembled", "seated", "proposed", "critique", "vote", "decided",
            "settles", "handoff", "returned", "asks"} <= set(kinds)
    # Two objections and a vote against: three split arrows, all from the critic.
    assert len(dissent) == 3
    assert {e["from"] for e in dissent} == {"party:agent:adversarial_reviewer"}
    # Every label is short (design §11.0): ≤ 4 words on a node, ≤ 3 on an edge.
    assert all(len(n["label"].split()) <= 4 for n in g["nodes"])
    assert all(len(e["label"].split()) <= 3 for e in g["edges"])
    # The Needs-you node hangs off the merge proposal it asks about.
    merge = next(n["id"] for n in g["nodes"] if n["label"] == "Merge")
    assert {"from": merge, "to": "needs_you"} in [{"from": e["from"], "to": e["to"]}
                                                    for e in g["edges"]]


def test_a_person_approves_the_merge_and_the_lead_resolves_the_item(eng, project):
    out = squad_demo.seed(eng, settings(), project_id=project, confirm_demo=True,
                          ensure_roles=True)
    item_id = out["item_id"]
    with eng.begin() as c:
        squad_turns.person_answered(c, item_id, actor="u_owner", approve=True,
                                    rationale="The dissent is noted; ship it.")
    ended = squad_turns.collaborate(eng, settings(), item_id, rounds=1, model=ScriptedSquadModel(
        {LEAD: [move("resolve", outcome="resolved",
                     summary="Patched in demo/web#88; merged by u_owner")]}))
    with eng.begin() as c:
        g = read_model.graph(c, item_id, project_id=project)
    assert ended["stopped"]["guard"] == "ended"
    assert ended["turns"][0]["learning"] or ended["turns"][0]["learning_skipped"] is None
    assert g["item"]["lifecycle"] == "resolved" and g["badges"]["outcome"] == "resolved_by_agents"
    assert any(n["type"] == "person" for n in g["nodes"])
    assert any(n["type"] == "outcome" and n["label"].startswith("Resolved") for n in g["nodes"])
