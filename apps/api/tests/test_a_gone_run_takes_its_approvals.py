"""A run that is stopped, removed or replaced takes its unanswered approvals with it.

SEEN LIVE ON boss-1 (2026-10-04): runs were stopped (POST /v1/workflow-runs/{id}/cancel) and
removed (DELETE /v1/delivery-workflows/runs/{id}), both 200 — and their gates stayed in
Approvals → "Needs action" and in the Command Center's count, 21 of them, each with Approve and
Reject. Approving one would release a step of a run that no longer exists.

Every test here goes through the endpoints the console uses, and reads the result from the
inbox and the Command Center the operator reads. See engine/boss_engine/services/run_decisions.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf


def _gate_then_agent(client, *, manual_agent: bool = False) -> dict:
    """An Approval gate, then an agent step — optionally one that "Needs approval", which opens
    a governance gate of its own (the "Create the tickets — …" cards on boss-1)."""
    skill = wf.skills(client, 1)[0]
    agent = wf.new_agent(client, wf.key("doer"), holds=(skill,))
    after = wf.agent_step("after", captain=agent, instructions={"value": "Carry on."})
    if manual_agent:
        after["approval"] = {"mode": "manual"}
    return wf.published(client, {"entry_step": "look", "steps": [
        wf.gate_step("look", nxt=("after",),
                     question=f"Go ahead with it? {uuid4().hex[:6]}"), after]})


def _at_the_gate(client, made: dict) -> tuple[str, str]:
    run_id = wf.started(client, made["definition"]["workflow_key"], {})
    wf.tick(client)
    view = wf.run_view(client, run_id)
    step = wf.steps_of(view)["look"]
    assert step["status"] == "waiting" and step["decision_id"], view
    return run_id, step["decision_id"]


def _cancel(client, run_id: str, reason: str = "Not needed any more.") -> None:
    stopped = client.post(f"/v1/workflow-runs/{run_id}/cancel", json={
        "project_id": wf.PROJECT, "actor": "ceo", "reason": reason})
    assert stopped.status_code == 200, stopped.text


def _remove(client, run_id: str) -> None:
    removed = client.delete(f"/v1/delivery-workflows/runs/{run_id}",
                            params={"project_id": wf.PROJECT, "actor": "ceo"})
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted"] is True


def _decision(client, decision_id: str) -> dict:
    got = client.get(f"/v1/decisions/{decision_id}")
    assert got.status_code == 200, got.text
    return got.json()


def _waiting_on_the_operator(client) -> tuple[set[str], set[str]]:
    """What the Approvals inbox lists under Needs action, and what the Command Center lists."""
    items = client.get("/v1/decisions/inbox", params={"project_id": wf.PROJECT}).json()["items"]
    inbox = {i["id"] for i in items if i["attention"] in ("needs_action", "expired")}
    cc = client.get("/v1/console/command-center", params={"project_id": wf.PROJECT}).json()
    listed = {n.get("decision_id") for n in cc["needs_you"] if n["kind"] == "approval"}
    return inbox, listed


def _approve(client, decision_id: str):
    return client.post(f"/v1/decisions/{decision_id}/approve",
                       json={"actor": "ceo", "note": "approved in the test"})


def _assert_withdrawn(client, decision_id: str, *, because: str) -> None:
    card = _decision(client, decision_id)
    assert card["status"] == "withdrawn", card
    assert because in (card["resolution_note"] or ""), card
    assert card["allowed_actions"] == [], card
    inbox, listed = _waiting_on_the_operator(client)
    assert decision_id not in inbox, "a withdrawn gate is still in Approvals"
    assert decision_id not in listed, "a withdrawn gate is still on the Command Center"
    refused = _approve(client, decision_id)
    assert refused.status_code == 409, refused.text
    assert "withdrawn" in refused.json()["detail"], refused.text
    assert _decision(client, decision_id)["status"] == "withdrawn"


def test_stopping_a_run_withdraws_its_approval_gate(client, monkeypatch):
    wf.install_model(monkeypatch)
    run_id, decision_id = _at_the_gate(client, _gate_then_agent(client))
    inbox, listed = _waiting_on_the_operator(client)
    assert decision_id in inbox and decision_id in listed, "the gate never reached Approvals"

    _cancel(client, run_id)

    _assert_withdrawn(client, decision_id, because="cancelled")
    # The run does not move on an answer it can no longer get.
    wf.tick(client)
    assert wf.statuses(wf.run_view(client, run_id))["after"] in ("pending", "cancelled")


def test_stopping_and_removing_a_run_withdraws_its_governance_gate(client, monkeypatch):
    """The "Needs approval" gate on a step (recommendation carries the run id), withdrawn by the
    stop, still withdrawn and still refused once the run is removed."""
    wf.install_model(monkeypatch)
    run_id, look = _at_the_gate(client, _gate_then_agent(client, manual_agent=True))
    assert _approve(client, look).status_code == 200
    for _ in range(2):
        wf.tick(client)
    after = wf.steps_of(wf.run_view(client, run_id))["after"]
    assert after["status"] == "waiting" and after["decision_id"], after
    gate = after["decision_id"]
    assert _decision(client, gate)["recommendation"]["workflow_run_id"] == run_id

    _cancel(client, run_id)
    _remove(client, run_id)

    _assert_withdrawn(client, gate, because="cancelled")


def test_stopping_a_parent_stops_its_child_and_withdraws_the_childs_gate(client, engine,
                                                                          monkeypatch):
    wf.install_model(monkeypatch)
    child = wf.published(client, {"entry_step": "ask", "steps": [wf.gate_step("ask", question=f"Help? {uuid4().hex[:6]}")]})
    with engine.begin() as c:
        version_id = c.execute(text(
            "SELECT active_version_id FROM workflow_definitions WHERE id = :d"),
            {"d": child["definition"]["id"]}).scalar()
    parent = wf.published(client, {"entry_step": "delegate", "steps": [
        {"id": "delegate", "kind": "subworkflow", "binding": {"workflow_version_id": version_id}}]})
    parent_run = wf.started(client, parent["definition"]["workflow_key"], {})
    for _ in range(3):
        wf.tick(client)
    with engine.begin() as c:
        child_run, gate = c.execute(text(
            "SELECT r.id, s.decision_id FROM workflow_runs r "
            "JOIN workflow_step_runs s ON s.workflow_run_id = r.id AND s.step_id = 'ask' "
            "WHERE r.parent_run_id = :p"), {"p": parent_run}).one()
    assert gate, "the child never reached its gate"

    _cancel(client, parent_run)

    assert wf.run_view(client, child_run)["status"] == "cancelled"
    _assert_withdrawn(client, gate, because="cancelled")


def test_a_gate_another_live_run_still_waits_on_is_not_withdrawn(client, monkeypatch):
    """create_blocking_decision hands two runs of one workflow one row for the same question.
    Stopping one must not take the other's question away."""
    wf.install_model(monkeypatch)
    made = _gate_then_agent(client)
    first, shared = _at_the_gate(client, made)
    second, also = _at_the_gate(client, made)
    assert also == shared, "the two runs no longer share a decision; this guard needs a new test"
    _cancel(client, first)
    assert _decision(client, shared)["status"] == "pending"
    assert _approve(client, shared).status_code == 200
    wf.tick(client)
    assert wf.statuses(wf.run_view(client, second))["look"] == "succeeded"


def test_answering_a_gate_whose_run_ended_some_other_way_is_refused(client, engine,
                                                                     monkeypatch):
    """A run that ended by a path that did not withdraw its gate (one that predates the rule)
    must still not have a step released by an answer."""
    wf.install_model(monkeypatch)
    run_id, decision_id = _at_the_gate(client, _gate_then_agent(client))
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_runs SET status = 'cancelled' WHERE id = :r"),
                  {"r": run_id})

    refused = _approve(client, decision_id)

    assert refused.status_code == 409, refused.text
    assert run_id in refused.json()["detail"] and "cancelled" in refused.json()["detail"]
    assert _decision(client, decision_id)["status"] == "pending", "the refusal acted anyway"


def test_the_repair_withdraws_gates_left_behind_by_runs_already_gone(client, engine,
                                                                      monkeypatch):
    """What boss-1 holds today: gates of runs stopped and removed before the rule, one of them a
    decision step whose only link back to its run was the step row the removal deleted."""
    from boss_engine.services.run_decisions import withdraw_orphaned

    wf.install_model(monkeypatch)
    made = _gate_then_agent(client)
    gone_run, legacy = _at_the_gate(client, made)
    stopped_run, stopped_gate = _at_the_gate(client, _gate_then_agent(client))
    live_run, live_gate = _at_the_gate(client, _gate_then_agent(client))
    with engine.begin() as c:
        # As a decision step wrote it before it recorded its run.
        c.execute(text("UPDATE decisions SET recommendation = recommendation - 'workflow_run_id' "
                       "- 'step_id' WHERE id = :d"), {"d": legacy})
        # Stopped and removed the old way: rows changed, decisions untouched.
        c.execute(text("UPDATE workflow_runs SET status = 'cancelled' WHERE id IN (:a, :b)"),
                  {"a": gone_run, "b": stopped_run})
        c.execute(text("DELETE FROM workflow_runs WHERE id = :r"), {"r": gone_run})
        # A governance gate whose run is long gone, as on boss-1.
        orphan = f"dec_{uuid4().hex[:20]}"
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status) "
            "VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending')"),
            {"i": orphan, "p": wf.PROJECT, "q": f"Create the tickets — {orphan}",
             "r": json.dumps({"capability": "agent", "step_id": "tickets",
                              "workflow_run_id": f"wfr_{uuid4().hex[:20]}"})})
    inbox, _ = _waiting_on_the_operator(client)
    assert {legacy, stopped_gate, orphan} <= inbox

    with engine.begin() as c:
        first = set(withdraw_orphaned(c, wf.PROJECT))
    with engine.begin() as c:
        again = withdraw_orphaned(c, wf.PROJECT)

    assert {legacy, stopped_gate, orphan} <= first
    assert live_gate not in first
    assert again == [], "the repair is not idempotent"
    _assert_withdrawn(client, legacy, because="removed")
    _assert_withdrawn(client, orphan, because="removed")
    _assert_withdrawn(client, stopped_gate, because="cancelled")
    assert _decision(client, live_gate)["status"] == "pending"
    assert live_run
