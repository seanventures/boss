"""A gate somebody answers NO fails its step, and the run stops there (agents-home D3, D4).

Rejecting a gate used to BLOCK the run with the gate still `waiting`: Live said "Waiting on a
person" about a person who had already answered, nothing offered a way forward, and the agent
the gate named was drawn amber (or Stuck) for a wait that was never its work. Now a no fails
the gate's step, the run stops at it like any failed step, and Retry (asks again: a fresh
decision) is offered.

What else is offered depends on what the no was about (workflow_run_actions.skip_refusal):
  * A STANDALONE GATE (a decision step) exists to release the steps after it. Skipping it
    would run exactly what the person refused, so it offers Stop, never Skip, and the server
    refuses a Skip with 409 — the steps after it never run.
  * A STEP HELD AT ITS OWN APPROVAL (a capability with `approval: manual`) may be skipped:
    the refused action never runs, and the run carries on without it, with a reason.
Pinned through the HTTP API the console and the worker use.
"""
from __future__ import annotations

import pytest

from apps.api.tests import workflow_management_support as wf


def _agent(client, prefix):
    return wf.new_agent(client, wf.key(prefix), holds=tuple(wf.skills(client, 1)))


def _run(client, monkeypatch, shape):
    wf.install_model(monkeypatch)
    writer = _agent(client, "scribe")
    pilot = _agent(client, "pilot")
    if shape == "decision":
        gate = wf.gate_step("publish", nxt=("wrap",), question="Publish it?")
    else:
        gate = wf.agent_step("publish", captain=pilot, nxt=("wrap",))
        gate["label"] = "Publish"
        gate["approval"] = {"mode": "manual", "risk_tier": 1}
    spec = {
        "entry_step": "draft",
        "input_schema": {"type": "object", "required": ["instructions", "title"],
                         "properties": {"instructions": {"type": "string"},
                                        "title": {"type": "string"}}},
        "steps": [wf.agent_step("draft", captain=writer, nxt=("publish",)), gate,
                  wf.agent_step("wrap", captain=writer)],
    }
    made = wf.published(client, spec)
    run_id = wf.started(client, made["definition"]["workflow_key"],
                        {"instructions": "Draft it.", "title": "Release notes 2.9"})
    for _ in range(4):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert wf.statuses(view)["publish"] == "waiting", wf.statuses(view)
    return run_id, pilot, wf.steps_of(view)["publish"]["decision_id"]


def _board(client):
    got = client.get("/v1/agents/live-board", params={"project_id": wf.PROJECT})
    assert got.status_code == 200, got.text
    return got.json()


def _reject(client, decision_id, note="not this week"):
    got = client.post(f"/v1/decisions/{decision_id}/reject",
                      json={"actor": "ceo", "note": note})
    assert got.status_code == 200, got.text


@pytest.mark.parametrize("shape", ["decision", "capability"])
def test_a_no_fails_the_step_and_offers_what_is_safe(client, monkeypatch, shape):
    run_id, pilot, decision_id = _run(client, monkeypatch, shape)
    # While it waits, the wait is the person's: the gate's hop names no agent.
    waiting = next(m for m in _board(client)["missions"] if m["run_id"] == run_id)
    gate_hops = [h for h in waiting["hops"] if h.get("gate")]
    assert gate_hops and all(h["role_key"] is None for h in gate_hops), waiting["hops"]
    assert all(not (h.get("role_key") == pilot and h["state"] == "needs_you")
               for h in waiting["hops"])

    _reject(client, decision_id)
    # AT ONCE, not on the next tick: the step failed and the run stopped there.
    view = wf.run_view(client, run_id)
    assert wf.statuses(view)["publish"] == "failed", wf.statuses(view)
    assert view["status"] == "failed", view["status"]

    board = _board(client)
    row = next(m for m in board["missions"] if m["run_id"] == run_id)
    assert row["state"] == "stopped", row
    card = next(a for a in board["attention"] if a["id"] == f"run:{run_id}:publish")
    assert card["kind"] == "failed_step"
    assert card["title"] == "“Publish” was rejected on Release notes 2.9", card["title"]
    assert card["detail"] == "not this week"
    second = {"decision": ("stop", "Stop this run"), "capability": ("skip_step", "Skip")}[shape]
    assert {a["key"]: a["label"] for a in card["answers"]} == {"retry_step": "Retry",
                                                                second[0]: second[1]}
    assert all(a["enabled"] for a in card["answers"])
    assert row["attention_id"] == card["id"]
    # A stopped run ranks first (D12); the starter is never stuck (D11).
    assert board["attention"][0]["kind"] == "failed_step"
    starter = next(h for h in row["hops"] if h.get("label") == "Started")
    assert starter["state"] != "stuck"
    # Nobody is "needs you" for an answered gate, and the pilot is not stuck for it.
    assert all(a["state"] not in ("needs_you", "stuck") for a in board["agents"]
               if a["role_key"] == pilot)
    # Activity says the person's no (D4).
    items = client.get("/v1/agents/activity", params={
        "project_id": wf.PROJECT, "run": run_id}).json()["items"]
    answers = [i for i in items if i["kind"] == "approval"]
    assert [i["verb"] for i in answers] == ["Rejected"], [(i["kind"], i["verb"]) for i in items]
    assert answers[0]["role_key"] is None
    assert not [i for i in items if i["role_key"] == pilot and i["verb"] in ("Stuck", "Needs you")]


@pytest.mark.parametrize("shape", ["decision", "capability"])
def test_retry_asks_again(client, monkeypatch, shape):
    run_id, _, decision_id = _run(client, monkeypatch, shape)
    _reject(client, decision_id)

    retried = client.post(f"/v1/delivery-workflows/runs/{run_id}/retry-step",
                          json={"project_id": wf.PROJECT, "step_id": "publish", "actor": "ceo"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["message"] == "Asking again: Publish"
    for _ in range(3):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    again = wf.steps_of(view)["publish"]
    assert again["status"] == "waiting" and again["decision_id"] not in (None, decision_id)


def test_a_refused_standalone_gate_is_never_skipped_past(client, monkeypatch):
    """A no on a gate whose job is to release what comes after it: Skip is refused (409, in a
    sentence) and the steps after it never run."""
    run_id, _, decision_id = _run(client, monkeypatch, "decision")
    _reject(client, decision_id)
    refused = client.post(f"/v1/delivery-workflows/runs/{run_id}/skip-step",
                          json={"project_id": wf.PROJECT, "step_id": "publish", "actor": "ceo",
                                "reason": "ship anyway"})
    assert refused.status_code == 409, refused.text
    assert "would run what you just refused" in str(refused.json()["detail"])
    for _ in range(4):
        wf.tick(client)
    statuses = wf.statuses(wf.run_view(client, run_id))
    assert statuses["publish"] == "failed", statuses
    assert statuses.get("wrap") in (None, "pending"), statuses


def test_a_refused_action_is_skipped_and_never_runs(client, monkeypatch):
    """A no on a step's OWN action: Skip carries on without it, and the action never ran."""
    run_id, _, decision_id = _run(client, monkeypatch, "capability")
    _reject(client, decision_id)

    # Skip needs a reason, and says so in a sentence.
    bare = client.post(f"/v1/delivery-workflows/runs/{run_id}/skip-step",
                       json={"project_id": wf.PROJECT, "step_id": "publish", "actor": "ceo"})
    assert bare.status_code == 422
    assert bare.json()["detail"].startswith("Say why you are skipping this step.")
    skipped = client.post(f"/v1/delivery-workflows/runs/{run_id}/skip-step",
                          json={"project_id": wf.PROJECT, "step_id": "publish", "actor": "ceo",
                                "reason": "ship without the post"})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["message"] == "Skipped Publish"
    for _ in range(4):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    publish = wf.steps_of(view)["publish"]
    # Skipped, never approved and never run: no agent work was recorded for it.
    assert publish["status"] == "skipped", publish
    assert not (publish.get("output") or {}).get("agent"), publish.get("output")
    assert wf.statuses(view)["wrap"] in ("succeeded", "running", "leased", "ready")
