"""THE INTEGRATED STORY (BOSSAPP-902): a signal becomes an item the agents work TOGETHER, end to
end, through the one path each stream built — no stage is called by hand.

    signal ─► Chief of Staff tick (A1: Needs-you "Hand to …?") ─► a person approves THROUGH THE
    DECISIONS SERVICE ─► answers.on_answered hands it over ─► the next tick seats a squad from
    the project's own agents ─► turns through the one agent pipeline (scripted engine) ─►
    consensus with a dissent ─► the gate says a person ─► Needs-you with the dissent on the
    card ─► the person approves ─► the squad carries the approval out (a workflow run) ─► the
    run's result comes back ─► the lead resolves ─► outcomes recorded ─► the read model draws
    every stage.

And the Order API incident through the Chief of Staff's routing (not its own intake): alert →
signal → CoS routes to the SRE squad → Needs you; the person's answer resumes the squad.
Plus the noise controls: a failed run that already asks a person gets no second card, and the
Chief of Staff raises at most three cards per class per project per day.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import text

from boss_engine.services.ambient import (board, chief_of_staff, collab, ledger, owners,
                                          read_model, signals)
from boss_engine.services.decisions import DecisionService

from ._sre_squad_support import eng, project  # noqa: F401  (purges incidents too)
from ._squad_support import (CRITIC, CVE_ROLES, ENGINEER, LEAD, Launchers, ScriptedSquadModel,
                             add_roles, move, settings)

PERSON = "ceo"


def _member(c, pid):
    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, :m, :m, 'human', '[\"ceo\"]'::jsonb, "
        "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{pid}_ceo", "p": pid, "m": PERSON})


def _answer(eng, decision_id, action="approve", note="Go ahead."):
    with eng.begin() as c:
        DecisionService(settings()).transition(c, decision_id, action=action, actor=PERSON,
                                               note=note)


def _tick(eng, pid, *, mode="propose", model=None, launchers=None, **kw):
    return chief_of_staff.tick(eng, settings(), project_id=pid, sweep=False, mode=mode,
                               squad_model=model, launchers=launchers, **kw)


def test_a_signal_is_worked_by_a_squad_from_triage_to_a_recorded_outcome(eng, project):
    pid = project
    with eng.begin() as c:
        add_roles(c, pid, CVE_ROLES)
        _member(c, pid)
        signals.record_signal(c, project_id=pid, source="scan", kind="scan.outdated_dependency",
                              title="lodash 4.17.20 is behind 4.17.21", subject_kind="dependency",
                              subject_id="lodash", app="web", severity="medium")
    # 1. The Chief of Staff triages: dependency upgrades run at A1 → it ASKS.
    first = _tick(eng, pid)
    [entry] = first["projects"][pid]["items"]
    assert entry["outcome"] == "proposed"
    item_id = entry["work_item_id"]
    with eng.begin() as c:
        item = board.get(c, item_id)
    assert item["lifecycle"] == "needs_you"
    assert owners.due_items(eng.connect(), project_id=pid) == []   # nobody works it yet

    # 2. A person approves the card where every decision is answered: the answer is carried
    # through at once (no tick needed), the item is handed to its owner.
    _answer(eng, item["needs_you_decision_id"])
    with eng.begin() as c:
        handoffs = collab.messages(c, item_id, kinds=("handoff",))
        cos_act = ledger.list_decisions(c, pid, work_item_id=item_id)[0]
    assert handoffs and handoffs[-1]["sender"] == chief_of_staff.PARTY
    assert cos_act["outcome"] == "accepted"

    # 3. The next tick: the squad is seated from the project's own agents and takes turns.
    #    The lead proposes a workflow, the engineer agrees, the critic OBJECTS (dissent);
    #    starting a workflow at A1 is a person's call → Needs you with the dissent on the card.
    model = ScriptedSquadModel({
        LEAD: [move("proposal", action="start_workflow", summary="Upgrade lodash to 4.17.21",
                    workflow="squad_demo_build"),
               move("pass"),
               move("resolve", outcome="resolved", summary="lodash 4.17.21 merged, CI green")],
        ENGINEER: [move("critique", position="agree", summary="Patch level, no API change")],
        CRITIC: [move("critique", position="object", summary="Add a test for /emails first")]})
    launchers = Launchers()
    second = _tick(eng, pid, model=model, launchers=launchers)
    worked = second["owners"]["worked"]
    assert [w["kind"] for w in worked] == ["squad"] and worked[0]["stopped"] == "needs_you"
    with eng.begin() as c:
        item = board.get(c, item_id)
        card = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                         {"d": item["needs_you_decision_id"]}).scalar()
        kinds = [m["kind"] for m in collab.messages(c, item_id)]
    assert item["lifecycle"] == "needs_you"
    assert "squad" in kinds and "proposal" in kinds and "critique" in kinds
    assert [d["party"] for d in card["dissent"]] == [CRITIC]
    assert card["asked_by"] == LEAD and card["proposal_id"]

    # 4. The person approves the squad's card: it is THEIR ledger row; the squad carries it out
    #    on the next tick, the run comes back, the lead resolves.
    _answer(eng, item["needs_you_decision_id"], note="Ship it; add the test in the PR.")
    with eng.begin() as c:
        person_rows = [d for d in ledger.list_decisions(c, pid, work_item_id=item_id)
                       if d["decided_by"] == f"person:{PERSON}" and d["action"]]
    assert [d["action"] for d in person_rows] == ["start_workflow"]
    third = _tick(eng, pid, model=model, launchers=launchers)
    assert third["owners"]["worked"][0]["kind"] == "squad"
    assert len(launchers.started) == 1, "the approval is carried out once"
    with eng.begin() as c:
        item = board.get(c, item_id)
    if item["lifecycle"] != "resolved":
        _tick(eng, pid, model=model, launchers=launchers)   # the result came back; lead ends it
    with eng.begin() as c:
        item = board.get(c, item_id)
        decisions = ledger.list_decisions(c, pid, work_item_id=item_id)
        g = read_model.graph(c, item_id, project_id=pid)
        listed = read_model.list_view(c, pid, filter_="all")["items"]
    assert item["lifecycle"] == "resolved"
    assert any(d["outcome"] for d in decisions), "outcomes land on the ledger"
    # 5. The diagram has every stage: signal → Chief of Staff → card → person → squad → members
    #    → proposal (with the dissent) → decision → run → outcome.
    types = {n["type"] for n in g["nodes"]}
    assert {"signal", "agent", "person", "squad", "action", "decision", "run",
            "outcome"} <= types
    assert any(e["dissent"] for e in g["edges"])
    edge_kinds = {e["kind"] for e in g["edges"]}
    assert {"raised", "assembled", "seated", "proposed", "critique", "decided"} <= edge_kinds
    # The list thumbnail shows the OWNER (the squad's lead), not the last agent who spoke.
    mine = next(i for i in listed if i["id"] == item_id)
    assert mine["owner"] == LEAD
    assert any(s["label"] == g_label(g, LEAD) for s in mine["strip"])


def g_label(g, party):
    return next(n["label"] for n in g["nodes"] if n["id"] == f"party:{party}")


def test_the_order_api_incident_goes_through_the_chief_of_staff_to_the_sre_squad(eng, project):
    from boss_engine.services import incident_squad_demo

    pid = project
    with eng.begin() as c:
        _member(c, pid)
        out = incident_squad_demo.run_through_chief_of_staff(c, pid, settings=settings())
        item_id = out["item_id"]
        decisions = ledger.list_decisions(c, pid, work_item_id=item_id)
        item = board.get(c, item_id)
    assert out["triage"].get("routed") == 1
    assert decisions[0]["decided_by"] == chief_of_staff.PARTY
    assert decisions[0]["chosen"] == "route:sre"
    assert out["stages"] == ["needs_you"] and item["lifecycle"] == "needs_you"
    # The person answers through the Decisions service: the SRE squad resumes on the same
    # scripted engine (the item is a demo) and the incident recovers.
    _answer(eng, item["needs_you_decision_id"], note="Roll back.")
    with eng.begin() as c:
        item = board.get(c, item_id)
        g = read_model.graph(c, item_id, project_id=pid)
    assert item["lifecycle"] in ("resolved", "proposed", "in_progress")
    labels = {n["label"] for n in g["nodes"]}
    assert "Chief of Staff" in labels and "SRE lead" in labels


def test_the_owners_tick_routes_an_incident_to_the_sre_squad(eng, project):
    """An incident the Chief of Staff routed is worked by the SRE squad on the owners' turn."""
    from boss_engine.services import incident_squad_demo as demo

    pid = project
    with eng.begin() as c:
        seeded = demo.seed(c, pid)
        signals.sweep(c, project_id=pid, only=["app_alerts"])
    report = _tick(eng, pid, mode="route", incident_mind=demo.demo_mind(),
                   incident_effects=demo.FixtureEffects())
    items = report["projects"][pid]["items"]
    assert [i["outcome"] for i in items] == ["routed"]
    assert [w["kind"] for w in report["owners"]["worked"]] == ["incident"]
    assert report["owners"]["worked"][0]["stage"] == "needs_you"
    assert seeded["incident_id"]


def test_a_failed_run_that_already_asks_a_person_gets_no_second_card(eng, project):
    pid = project
    with eng.begin() as c:
        from boss_engine.services.core import create_blocking_decision

        run_id = "wfr_demo_asks"
        dec = create_blocking_decision(c, pid, None, "Approve the deploy?",
                                       {"action": "resume_workflow", "workflow_run_id": run_id},
                                       {}, "a gate", risk_tier=1)
        signals.record_signal(c, project_id=pid, source="workflow", kind="workflow.step_failed",
                              title="Step deploy failed in Ship", subject_kind="workflow_run",
                              subject_id=run_id, severity="medium")
    report = _tick(eng, pid)
    [entry] = report["projects"][pid]["items"]
    assert entry["outcome"] == "linked" and entry["decision_id"] == dec
    with eng.begin() as c:
        item = board.get(c, entry["work_item_id"])
        links = {(l["kind"], l["ref"]) for l in board.links(c, item["id"])}
    assert item["lifecycle"] != "needs_you" and ("decision", dec) in links


def test_the_chief_of_staff_asks_at_most_three_cards_per_class_a_day(eng, project):
    pid = project
    with eng.begin() as c:
        for n in range(5):
            signals.record_signal(c, project_id=pid, source="pr", kind="pr.stale",
                                  title=f"PR #{n} waiting 4d", subject_kind="pull_request",
                                  subject_id=f"pr:acme/web#{n}", severity="low")
    report = _tick(eng, pid)
    outcomes = sorted(i["outcome"] for i in report["projects"][pid]["items"])
    assert outcomes == ["capped", "capped", "proposed", "proposed", "proposed"]


def test_an_install_wide_finding_is_triaged_in_the_default_project(eng, project):
    pid = project
    with eng.begin() as c:
        got = signals.record_signal(c, project_id=None, source="doctor", kind="doctor.disk_low",
                                    title="Disk 92% full", subject_kind="host",
                                    subject_id="install", severity="high")["signal"]
        moved = chief_of_staff.adopt_install_wide(c, SimpleNamespace(default_project_id=pid))
        row = c.execute(text("SELECT project_id FROM signals WHERE id = :s"),
                        {"s": got["id"]}).scalar()
    assert moved >= 1 and row == pid


def test_load_demo_populates_every_story_on_a_fresh_demo_project(eng, project, monkeypatch):
    from boss_engine.services.ambient import demo

    monkeypatch.setenv("BOSS_AMBIENT_DEMO", "1")
    monkeypatch.setenv("BOSS_DEMO_DATA", "1")
    out = demo.load_all(eng, settings(), project_id=project, actor=PERSON)
    assert out["skipped"] == {}, out["skipped"]
    assert out["loaded"]["order_api_incident"]["stages"] == ["needs_you"]
    assert out["loaded"]["squad_cve"]["stopped"] == "needs_you"
    with eng.begin() as c:
        listed = read_model.list_view(c, project, filter_="all")
        incident = board.get(c, out["loaded"]["order_api_incident"]["item_id"])
        joined = [l for l in board.links(c, incident["id"]) if l["kind"] == "signal"]
    assert listed["counts"]["needs_you"] >= 3
    # The morning's two order-api alerts JOIN the incident the Chief of Staff already routed.
    assert len(joined) >= 2
    again = demo.load_all(eng, settings(), project_id=project, actor=PERSON)
    assert "order_api_incident" in again["skipped"]
