"""The Order API incident replays end to end, and the console can SEE the agents collaborate
(BOSSAPP-910; design §12.6 "Demo (Phase 1 exit)", §11.0 "visibility is a diagram").

* Production: the alert is swept into a signal, the Chief-of-Staff routes it, the squad forms,
  finds the changed function on the failing route, recalls the earlier incident, recommends a
  rollback with one dissent, and asks a person — drawn as a flow: signal → CoS → SRE → squad →
  proposal with a split (dissent) arrow → Needs you.
* Approved from the card, the same item ends resolved: the person's decision, the fix's pull
  request, the outcome — and every decision on the way cites evidence.
* Every node label is at most four words; the list shows the item as a strip.
* The scripted engine makes no model call; the CLI prints the flow.
"""
from __future__ import annotations

import json

from boss_engine.config import get_settings
from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import ledger, read_model
from boss_engine.services.decisions import DecisionService

from ._sre_squad_support import add_person, eng, project  # noqa: F401


def _types(graph):
    return [n["type"] for n in graph["nodes"]]


def test_the_production_replay_draws_the_squad_up_to_the_persons_decision(eng, project):
    with eng.begin() as c:
        out = demo.run_scenario(c, project)
    g = out["graph"]
    assert out["stages"] == ["needs_you"]
    labels = {n["label"] for n in g["nodes"]}
    assert {"Chief of Staff", "SRE", "Operations Engineer", "Backend Engineer",
            "Adversarial Reviewer", "Squad", "Roll back prod", "Needs you"} <= labels
    assert {"Release 6bf2672", "Reuse INC-31 fix", "Recommend rollback"} <= labels
    assert _types(g)[0] == "signal" and "needs_you" in _types(g)
    dissent = [e for e in g["edges"] if e["dissent"]]
    assert [(e["from"], e["kind"], e["label"]) for e in dissent] == [
        ("party:agent:adversarial_reviewer", "vote", "reject")]
    assert g["badges"]["needs_you"] and g["badges"]["dissent"]
    assert g["badges"]["problem_type"] == "Incident"
    assert all(len(n["label"].split()) <= 4 for n in g["nodes"])
    assert all(len(e["label"].split()) <= 3 for e in g["edges"])


def test_approved_from_the_card_the_replay_ends_resolved_with_every_step_drawn(eng, project):
    with eng.begin() as c:
        out = demo.run_scenario(c, project)
        add_person(c, project)
        DecisionService(get_settings()).transition(c, out["needs_you_decision_id"],
                                                   action="approve", actor="ceo", note=None)
        g = read_model.graph(c, out["item_id"], project_id=project)
        decisions = ledger.list_decisions(c, project, work_item_id=out["item_id"])
        listed = read_model.list_view(c, project, filter_="all")
    types = _types(g)
    assert types[-1] == "outcome" and g["item"]["lifecycle"] == "resolved"
    assert {"person", "run", "decision", "action"} <= set(types)
    assert {"Pull request", "Incident", "Recovered"} <= {n["label"] for n in g["nodes"]}
    assert [d["question"] for d in decisions] == [
        "Who works this signal?", squad.Q_TRIAGE, squad.Q_CAUSE, squad.Q_LEARN,
        squad.Q_MITIGATE, squad.Q_MITIGATE, squad.Q_FIX, squad.Q_VERIFY, squad.Q_CLOSE]
    for d in decisions:
        assert d["evidence"], d["question"]
        assert ledger.sentences(d["rationale"]) <= 3
    row = next(i for i in listed["items"] if i["id"] == out["item_id"])
    assert [s["type"] for s in row["strip"]][0] == "signal"
    assert row["strip"][-1]["type"] == "outcome"


def test_the_staging_replay_needs_nobody(eng, project):
    with eng.begin() as c:
        out = demo.run_scenario(c, project, environment="staging")
    g = out["graph"]
    assert out["stages"] == ["resolved"]
    assert "needs_you" not in _types(g) and "person" not in _types(g)
    assert g["badges"]["outcome"] == "resolved_by_agents"
    assert g["badges"]["autonomy_level"] == "A2"


def test_the_scripted_engine_answers_every_judgement_without_a_model(eng, project):
    mind = demo.demo_mind()
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        squad.route_incident(c, project_id=project, item_id=prepared["item_id"], mind=mind,
                             effects=demo.FixtureEffects())
    assert mind.calls == [("agent:operations_engineer", "verdict"),
                          ("agent:adversarial_reviewer", "critique"),
                          ("agent:operations_engineer", "vote"),
                          ("agent:backend_engineer", "vote"),
                          ("agent:adversarial_reviewer", "vote")]
    assert len(mind.calls) <= squad.MAX_MODEL_CALLS


def test_the_cli_loads_the_scenario_and_prints_the_flow(eng, project, capsys, monkeypatch):
    import os

    monkeypatch.setenv("DATABASE_URL", os.environ["BOSS_TEST_DATABASE_URL"])
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    try:
        assert demo.main(["--project", project, "--env", "staging"]) == 0
    finally:
        if hasattr(get_settings, "cache_clear"):
            get_settings.cache_clear()
    out = capsys.readouterr().out
    printed = json.loads(out[out.index("{"):])
    assert printed["stages"] == ["resolved"]
    assert printed["flow"][0].startswith("0 signal")
    assert printed["flow"][-1].startswith("5 outcome")
