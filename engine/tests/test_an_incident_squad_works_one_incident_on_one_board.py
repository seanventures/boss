"""An incident squad works ONE incident on ONE board (BOSSAPP-910; design §12.2-12.4).

* An alert reaches the squad the real way: the app_alerts adapter makes it a signal, the
  Chief-of-Staff opens one item, records why it routed it and hands it to the SRE lead.
* The squad forms: the lead claims the item, hands the investigator, the app's engineer and the
  reviewer their jobs, and every seat speaks only in typed board messages.
* Root cause: the failing route is followed through the code graph to the functions the
  serving release changed; every hypothesis and the verdict cite evidence BOSS checked exists.
* A second alert on the same app joins the incident already being worked; routing an item twice
  never runs the squad twice.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import board, collab, ledger
from boss_engine.services.ambient._common import EvidenceError

from ._sre_squad_support import eng, project  # noqa: F401


def _run(c, pid, env="production"):
    prepared = demo.prepare(c, pid, environment=env)
    out = squad.route_incident(c, project_id=pid, item_id=prepared["item_id"],
                               mind=demo.demo_mind(env), effects=demo.FixtureEffects())
    return prepared, out


def test_an_alert_reaches_the_squad_as_one_routed_item(eng, project):
    with eng.begin() as c:
        prepared = demo.prepare(c, project)
        item = board.get(c, prepared["item_id"])
        decisions = ledger.list_decisions(c, project, work_item_id=item["id"])
        handoffs = collab.messages(c, item["id"], kinds=("handoff",))
        sig = c.execute(text("SELECT source, kind, work_item_id FROM signals WHERE id = :s"),
                        {"s": prepared["signal_id"]}).mappings().one()
    assert (sig["source"], sig["kind"], sig["work_item_id"]) == ("alert", "alert.firing",
                                                                item["id"])
    assert item["task_class"] == "incident_response" and item["app"] == "order-api"
    assert item["created_by"] == "agent:chief_of_staff"
    assert [(d["decided_by"], d["chosen"]) for d in decisions] == [
        ("agent:chief_of_staff", "SRE squad")]
    assert decisions[0]["evidence"][0] == {"kind": "signal", "ref": prepared["signal_id"],
                                           "checked": "exists"}
    assert handoffs[0]["body"]["to"] == "agent:sre"


def test_a_staging_alert_opens_a_staging_mitigation_item(eng, project):
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        item = board.get(c, prepared["item_id"])
    assert item["task_class"] == "incident_staging_mitigation"
    assert item["autonomy_level"] == "A2"


def test_the_squad_forms_and_every_seat_speaks_in_typed_messages(eng, project):
    with eng.begin() as c:
        prepared, out = _run(c, project)
        item = board.get(c, prepared["item_id"])
        msgs = collab.messages(c, item["id"])
    seats = squad.Squad().seats()
    assert out["stage"] == "needs_you"
    assert set(seats.values()) <= set(item["squad"])
    by_kind: dict[str, set[str]] = {}
    for m in msgs:
        by_kind.setdefault(m["kind"], set()).add(m["sender"])
    assert by_kind["claim"] == {seats["lead"]}
    assert seats["investigator"] in by_kind["hypothesis"]
    assert seats["engineer"] in by_kind["finding"]
    assert by_kind["critique"] == {seats["critic"]}
    assert by_kind["vote"] == {seats["investigator"], seats["engineer"], seats["critic"]}
    handed = [m["body"]["to"] for m in msgs if m["kind"] == "handoff" and
              m["sender"] == seats["lead"]]
    assert handed == [seats["investigator"], seats["engineer"], seats["critic"]]


def test_the_failing_route_is_narrowed_to_the_functions_the_release_changed(eng, project):
    with eng.begin() as c:
        seeded = demo.seed(c, project)
        code = squad.route_to_code(c, project, "POST /orders",
                                   ["app/pricing.py", "app/coupons.py"])
        nothing = squad.route_to_code(c, project, "GET /nowhere", ["app/pricing.py"])
    names = {n["name"] for n in code["changed"]}
    assert code["endpoint"]["id"] == seeded["code"]["endpoint"]
    assert {"order_total", "bulk_discount"} <= names
    assert "create_order" not in names                  # on the path, but not changed
    assert code["dependents"] >= 1                      # invoices.reprice calls order_total
    assert nothing["endpoint"] is None and nothing["changed"] == []


def test_every_root_cause_claim_cites_evidence_that_exists(eng, project):
    with eng.begin() as c:
        prepared, _ = _run(c, project)
        item_id = prepared["item_id"]
        hyps = collab.messages(c, item_id, kinds=("hypothesis",))
        cause = [d for d in ledger.list_decisions(c, project, work_item_id=item_id)
                 if d["question"] == squad.Q_CAUSE]
        bulk = prepared["code"]["app.pricing.bulk_discount"]
        with pytest.raises(EvidenceError, match="does not exist"):
            collab.post(c, item_id, kind="hypothesis", sender="agent:operations_engineer",
                        body={"summary": "a ghost", "confidence": 0.9,
                              "evidence": [{"kind": "kg_node", "ref": "kgn_ghost"}]})
    assert len(hyps) >= 2
    for h in hyps:
        assert h["body"]["evidence"], h
        assert all(e["checked"] in ("exists", "shape") for e in h["body"]["evidence"])
    release = next(h for h in hyps if h["body"]["summary"].startswith("Release 6bf2672"))
    refs = {(e["kind"], e["ref"]) for e in release["body"]["evidence"]}
    assert ("kg_node", bulk) in refs and ("commit", demo.SERVING) in refs
    assert len(cause) == 1 and cause[0]["chosen"] == "Release 6bf2672"
    assert cause[0]["decided_by"] == "agent:operations_engineer"
    assert ledger.sentences(cause[0]["rationale"]) <= 3


def test_a_second_alert_on_the_same_app_joins_the_incident_being_worked(eng, project):
    from boss_engine.services.ambient import signals

    with eng.begin() as c:
        first, _ = _run(c, project)
        sig = signals.record_signal(
            c, project_id=project, source="alert", kind="alert.firing",
            title="order-api p95 latency 4s", fingerprint_parts=("latency", "order-api"),
            subject_kind="app", subject_id="order-api", app="order-api", severity="high")
        second = squad.intake(c, project_id=project, signal_id=sig["signal"]["id"])
        out = squad.route_incident(c, project_id=project, item_id=second["item"]["id"],
                                   mind=demo.demo_mind(), effects=demo.FixtureEffects())
        joined = board.get(c, second["item"]["id"])
        links = {(l["kind"], l["ref"]) for l in board.links(c, first["item_id"])}
    assert out["stage"] == "joined" and out["joined"] == first["item_id"]
    assert joined["lifecycle"] == "dismissed" and first["item_id"] in joined["outcome"]
    assert ("signal", sig["signal"]["id"]) in links


def test_routing_an_item_twice_never_runs_the_squad_twice(eng, project):
    with eng.begin() as c:
        prepared, _ = _run(c, project)
        again = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                     mind=demo.demo_mind(), effects=demo.FixtureEffects())
        triage = [d for d in ledger.list_decisions(c, project,
                                                   work_item_id=prepared["item_id"])
                  if d["question"] == squad.Q_TRIAGE]
    assert again["stage"] == "needs_you"
    assert len(triage) == 1


def test_a_non_incident_item_is_not_the_squads(eng, project):
    from ._ambient_support import open_item

    with eng.begin() as c:
        item = open_item(c, project, task_class="doc_drift", fingerprint="fp:docs")["item"]
        with pytest.raises(ValueError, match="not an incident class"):
            squad.route_incident(c, project_id=project, item_id=item["id"],
                                 mind=squad.RuleMind(), effects=demo.FixtureEffects())
