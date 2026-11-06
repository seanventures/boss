"""Agent Operations draws each work item as a flow, not a page of text (BOSSAPP-906/907; design
§11.0 — the operator's standing rule — §11.3, §11.7).

The D2 incident, end to end on the board: an alert raises it, the Chief-of-Staff routes it, the
SRE claims it, the investigator posts a hypothesis, the SRE proposes a rollback, two agree and
one dissents, the person approves from Needs you, the item resolves. The graph must read left to
right as signal → who → what they proposed → how they agreed → the decision → the outcome, with
labels of at most four words, the dissent as a flagged edge, and the outcome, Needs you, level
and cost as badges. The list is filtered into the five tabs and every item carries a strip for
its thumbnail. The scorecard is computed from those rows.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services.ambient import board, collab, ledger, policy, read_model

from ._ambient_support import eng, open_item, project  # noqa: F401


def _d2(c, pid: str) -> dict:
    policy.set_level(c, pid, "incident_response", actor="ceo", level="A3")
    item = open_item(c, pid)["item"]
    token = board.claim(c, item["id"], owner="agent:sre")["lease_token"]
    collab.post(c, item["id"], kind="hypothesis", sender="agent:investigator",
                body={"summary": "N+1 in total()", "confidence": 0.72,
                      "evidence": [{"kind": "commit", "ref": "6bf2673"}]})
    prop = collab.post(c, item["id"], kind="proposal", sender="agent:sre",
                       body={"action": "prod_rollback", "summary": "Roll back to 6bf2672"})
    for voter, vote in (("agent:investigator", "approve"), ("agent:software_engineer", "approve"),
                        ("agent:adversarial_reviewer", "reject")):
        collab.post(c, item["id"], kind="vote", sender=voter, reply_to=prop["id"],
                    body={"vote": vote, "reason": "drops the coupon fix" if vote == "reject"
                          else ""})
    consensus = collab.tally(c, item["id"], prop["id"])
    board.needs_you(c, item["id"], actor="agent:sre", token=token,
                    question="Roll back order-api to 6bf2672?",
                    recommendation={"action": "prod_rollback", "consensus": consensus})
    return {"item": item, "token": token, "proposal": prop, "consensus": consensus}


def test_an_item_that_needs_you_draws_an_amber_needs_you_node_after_the_vote(eng, project):
    with eng.begin() as c:
        d2 = _d2(c, project)
        g = read_model.graph(c, d2["item"]["id"], project_id=project)
    types = [n["type"] for n in g["nodes"]]
    assert types[0] == "signal" and types[-1] == "needs_you"
    assert [n["stage"] for n in g["nodes"]] == sorted(n["stage"] for n in g["nodes"])
    assert all(len(n["label"].split()) <= 4 for n in g["nodes"])
    assert all(len(e["label"].split()) <= 3 for e in g["edges"])
    votes = [e for e in g["edges"] if e["kind"] == "vote"]
    assert len(votes) == 3 and [e["dissent"] for e in votes].count(True) == 1
    assert g["badges"]["outcome"] == "needs_you" and g["badges"]["needs_you"]
    assert g["badges"]["problem_type"] == "Incident" and g["badges"]["autonomy_level"] == "A3"
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    assert labels["party:agent:sre"] == "SRE" and labels["party:agent:chief_of_staff"] == \
        "Chief of Staff"
    assert "1 finding" in next(n for n in g["nodes"]
                               if n["id"] == "party:agent:investigator")["badges"]
    assert next(n for n in g["nodes"] if n["type"] == "action")["label"] == "Roll back prod"


def test_a_resolved_item_ends_in_a_decision_by_a_person_and_an_outcome_node(eng, project):
    with eng.begin() as c:
        d2 = _d2(c, project)
        item_id = d2["item"]["id"]
        decision = ledger.record_decision(
            c, project_id=project, decided_by="person:ceo", question="Roll back order-api?",
            chosen="rollback", options=["rollback", "wait"], rationale="Customers are failing.",
            reversibility="reversible", undo={"action": "redeploy 6bf2673"},
            work_item_id=item_id, action="prod_rollback", consensus=d2["consensus"],
            proposal_id=d2["proposal"]["id"], cost_usd_micros=1_840_000)
        board.answered(c, item_id, actor="ceo")
        board.link(c, item_id, "workflow_run", "wfr_rollback_1")
        board.finish(c, item_id, actor="agent:sre", token=d2["token"], lifecycle="resolved",
                     outcome="recovered")
        g = read_model.graph(c, item_id, project_id=project)
    kinds = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    did = f"decision:{decision['act_id']}"
    assert ("party:person:ceo", did, "decided") in kinds
    assert (f"proposal:{d2['proposal']['id']}", did, "settles") in kinds
    assert (did, "workflow_run:wfr_rollback_1", "acted") in kinds
    assert ("workflow_run:wfr_rollback_1", "outcome", "resulted") in kinds
    decision_node = next(n for n in g["nodes"] if n["id"] == did)
    assert "dissent" in decision_node["badges"] and "person" in decision_node["badges"]
    assert g["nodes"][-1]["type"] == "outcome" and g["nodes"][-1]["label"].startswith("Resolved")
    assert g["badges"]["outcome"] == "resolved_by_agents" and g["badges"]["cost_usd"] == 1.84
    assert g["badges"]["dissent"] is True
    assert all(n.get("ref") for n in g["nodes"]), "every node opens its detail in one click"


def test_the_list_filters_into_the_five_tabs_and_each_item_carries_a_strip(eng, project):
    with eng.begin() as c:
        d2 = _d2(c, project)
        running = open_item(c, project, fingerprint="fp:docs", task_class="doc_drift")["item"]
        failed = open_item(c, project, fingerprint="fp:fail", task_class="flaky_test")["item"]
        tok = board.claim(c, failed["id"], owner="agent:qa")["lease_token"]
        board.start(c, failed["id"], owner="agent:qa", token=tok)
        board.finish(c, failed["id"], actor="agent:qa", token=tok, lifecycle="failed")
        overridden = open_item(c, project, fingerprint="fp:over", task_class="doc_drift")["item"]
        row = ledger.record_decision(
            c, project_id=project, decided_by="agent:docs_writer", question="Worth fixing?",
            chosen="yes", rationale="README is wrong.", reversibility="compensable",
            evidence=[{"kind": "work_item", "ref": overridden["id"]}],
            work_item_id=overridden["id"])
        ledger.override(c, row["act_id"], actor="ceo", chosen="no", rationale="Docs are frozen.")
        views = {f: read_model.list_view(c, project, filter_=f)
                 for f in ("needs_you", "running", "failed", "overridden", "all")}
    assert [i["id"] for i in views["needs_you"]["items"]] == [d2["item"]["id"]]
    assert {i["id"] for i in views["running"]["items"]} == {running["id"], overridden["id"]}
    assert [i["id"] for i in views["failed"]["items"]] == [failed["id"]]
    assert [i["id"] for i in views["overridden"]["items"]] == [overridden["id"]]
    assert views["all"]["counts"] == {"needs_you": 1, "running": 2, "resolved_by_agents": 0,
                                      "overridden": 1, "failed": 1, "dismissed": 0, "all": 4}
    strip = views["needs_you"]["items"][0]["strip"]
    assert [s["type"] for s in strip][0] == "signal" and strip[-1]["type"] == "needs_you"
    assert len(strip) <= 6


def test_the_scorecard_is_computed_from_the_ledger_and_the_board(eng, project):
    with eng.begin() as c:
        d2 = _d2(c, project)
        item_id = d2["item"]["id"]
        c.execute(text("UPDATE work_items SET needs_you_at = now() - interval '30 minutes', "
                       "created_at = now() - interval '45 minutes' WHERE id = :w"),
                  {"w": item_id})
        board.answered(c, item_id, actor="ceo")
        board.add_cost(c, item_id, 1_840_000)
        board.finish(c, item_id, actor="agent:sre", token=d2["token"], lifecycle="resolved")
        docs = open_item(c, project, fingerprint="fp:docs", task_class="doc_drift")["item"]
        d = ledger.record_decision(
            c, project_id=project, decided_by="agent:docs_writer", question="Fix README?",
            chosen="yes", rationale="Stale env var.", reversibility="compensable",
            evidence=[{"kind": "work_item", "ref": docs["id"]}], work_item_id=docs["id"],
            action="propose")
        ledger.record_outcome(c, d["act_id"], outcome="accepted")
        board.dismiss(c, docs["id"], actor="ceo", reason="already fixed upstream")
        card = read_model.scorecard(c, project, days=7)
    today = card["decisions_per_day"][-1]
    assert today["total"] == 1 and today["agent"] == 1 and today["a1"] == 1
    assert card["by_class"] == [{"task_class": "doc_drift", "decided": 1, "accepted_pct": 100.0,
                                 "overridden_pct": 0.0, "reverted_pct": 0.0}]
    assert card["time_to_human_decision_minutes"]["n"] == 1
    assert 29 <= card["time_to_human_decision_minutes"]["median"] <= 31
    assert card["mttr_minutes"]["n"] == 1 and card["incident_mttr_minutes"]["n"] == 1
    assert card["items_ended"] == {"resolved": 1, "dismissed": 1}
    assert card["noise_rate"] == 0.5
    assert card["cost"]["spent_usd"] == 1.84 and card["cost"]["per_resolved_item_usd"] == 1.84
    assert card["stopped"]["stopped"] is False
