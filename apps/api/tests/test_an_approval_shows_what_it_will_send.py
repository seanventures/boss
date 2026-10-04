"""A gated email step's decision shows the email, and approves exactly that email.

Found running the bundled examples live: the newsletter's send step was held with
"Email the newsletter: weekly_newsletter · run 4715a965", a "staging" environment and
"Approve and retry" — and nothing about who the email went to or what it said.

Driven through the API the console and the worker use: built with `/publish`, started with
`/start`, advanced by `/run-capabilities`, answered through `/v1/decisions`, and read back from
the decisions inbox and the runs view. Only the model behind the writing step is scripted.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf

EMAIL_SKILL = "email.send_real_guarded"


def _build(client) -> dict:
    enabled = client.post(f"/v1/skills/{EMAIL_SKILL}/enable",
                          json={"project_id": wf.PROJECT, "actor": "ceo"})
    assert enabled.status_code in (200, 201), enabled.text
    agent = wf.new_agent(client, wf.key("publisher"), holds=(EMAIL_SKILL,))
    made = client.post("/v1/delivery-workflows/custom-steps", json={
        "project_id": wf.PROJECT, "actor": "ceo", "binding_kind": "skill",
        "label": "Email the newsletter", "summary": "Send it, once a person has read it.",
        "skill_id": EMAIL_SKILL,
        "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                   {"name": "subject", "required": False}]})
    assert made.status_code in (200, 201), made.text
    step_key = (made.json().get("custom_step") or made.json().get("step") or made.json())[
        "step_key"]
    spec = {
        "entry_step": "write",
        "input_schema": {"type": "object", "required": ["instructions", "to"],
                         "properties": {"instructions": {"type": "string"},
                                        "to": {"type": "string", "format": "email"},
                                        "title": {"type": "string"}}},
        "steps": [
            wf.agent_step("write", captain=agent, nxt=("send",)),
            {"id": "send", "kind": "capability", "captain": agent, "next": [],
             "max_attempts": 1,
             "binding": {"custom_step": step_key, "inputs": {
                 "to": {"from_input": "to"},
                 "subject": {"from_input": "subject", "default": "Your weekly newsletter"},
                 "body": {"from_step": "write", "field": "summary"}}},
             "approval": {"mode": "manual"}},
        ],
    }
    made = wf.published(client, spec)
    return {"agent": agent, "workflow_key": made["definition"]["workflow_key"]}


def _decision(client, decision_id: str) -> dict:
    found = client.get(f"/v1/decisions/{decision_id}", params={"project_id": wf.PROJECT})
    assert found.status_code == 200, found.text
    return found.json()


def _in_the_inbox(client, decision_id: str) -> dict:
    """The decision as the Approvals area lists it."""
    items = client.get("/v1/decisions/inbox", params={"project_id": wf.PROJECT,
                                                      "limit": 200}).json()["items"]
    found = [d for d in items if d["id"] == decision_id]
    assert found, f"decision {decision_id} is not in the inbox"
    return found[0]


def _rewrite(engine, run_id: str, words: str) -> None:
    """What re-running the writing step does to its output, without paying a model."""
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_step_runs SET output = output || CAST(:o AS JSONB) "
                       "WHERE workflow_run_id = :r AND step_id = 'write'"),
                  {"r": run_id, "o": json.dumps({"summary": words})})


def test_the_decision_shows_the_email_and_approves_only_that_email(client, engine, monkeypatch):
    wf.install_model(monkeypatch)
    built = _build(client)
    # Started the way "Start anyway" does: no model (it is scripted) and no SMTP server (the
    # email is never meant to leave this test).
    started = wf.start(client, built["workflow_key"], {
        "instructions": "Write this week's newsletter.", "to": "reader@example.com",
        "title": "E2E: Weekly newsletter"}, start_anyway=["model", "email"])
    assert started.status_code == 200, started.text
    run_id = started.json()["run"]["id"]
    wf.tick(client)  # writes
    wf.tick(client)  # opens the send step's gate
    view = wf.run_view(client, run_id)
    assert wf.statuses(view) == {"write": "succeeded", "send": "waiting"}, view
    send = wf.steps_of(view)["send"]
    decision_id = send["decision_id"]
    assert decision_id

    # ── The decision, as the Approvals area reads it ──────────────────────────────────────────
    decision = _in_the_inbox(client, decision_id)
    assert decision["question"] == "Email the newsletter — E2E: Weekly newsletter"
    assert "weekly_newsletter" not in decision["question"] and run_id[-8:] not in decision[
        "question"]
    rec = decision["recommendation"]
    assert rec["workflow_run_id"] == run_id, "the ids stay, as evidence"
    assert "environment" not in rec, "an email goes to a person, not to an environment"
    preview = rec["action_preview"]
    assert preview["kind"] == "email"
    assert preview["skill_id"] == EMAIL_SKILL
    assert preview["to"] == ["reader@example.com"]
    assert preview["subject"] == "Your weekly newsletter"
    assert preview["body"] == "Done.", "the body is what the writing step actually produced"
    assert preview["content_hash"].startswith("sha256:")
    labels = [o["label"] for o in decision["options"]]
    assert labels == ["Approve", "Reject"], labels
    assert [a["label"] for a in decision["allowed_actions"]][:2] == ["Approve", "Reject"]
    # And the run's step carries the same preview, for the step's own card.
    assert send["action_preview"]["content_hash"] == preview["content_hash"]

    # ── The writing step is re-run while the approver reads: the preview follows ─────────────
    seen = preview["content_hash"]
    _rewrite(engine, run_id, "A different newsletter.")
    wf.tick(client)
    refreshed = _decision(client, decision_id)["recommendation"]["action_preview"]
    assert refreshed["body"] == "A different newsletter."
    assert refreshed["content_hash"] != seen

    # An approval of what they saw BEFORE the change is refused, in words.
    stale = client.post(f"/v1/decisions/{decision_id}/approve",
                        json={"actor": "ceo", "note": "ok", "content_hash": seen})
    assert stale.status_code == 409, stale.text
    assert "changed while you were reviewing" in stale.json()["detail"]
    assert _decision(client, decision_id)["status"] == "pending"

    # Approving what is shown now is recorded against that content.
    approved = client.post(f"/v1/decisions/{decision_id}/approve",
                           json={"actor": "ceo", "note": "ok",
                                 "content_hash": refreshed["content_hash"]})
    assert approved.status_code == 200, approved.text
    assert _decision(client, decision_id)["recommendation"]["approved_content_hash"] == \
        refreshed["content_hash"]

    # ── And if the content changes AFTER the approval, nothing is sent ───────────────────────
    _rewrite(engine, run_id, "Words nobody approved.")
    wf.tick(client)
    send = wf.steps_of(wf.run_view(client, run_id))["send"]
    # Settled, not retried on a schedule: the same approval would only refuse again.
    assert send["status"] in ("failed", "deadlettered"), send
    assert "changed after it was approved" in (send["error"] or "")
    assert "Nothing was sent" in send["error"]
    # Retry asks again, about the new content: the old approval no longer releases it.
    assert send["decision_id"] is None
    assert send["action_preview"] is None

    # Retry opens a fresh gate, showing the content that would be sent now.
    retried = wf.post(client, f"runs/{run_id}/retry-step", step_id="send")
    assert retried.status_code == 200, retried.text
    wf.tick(client)
    send = wf.steps_of(wf.run_view(client, run_id))["send"]
    assert send["status"] == "waiting" and send["decision_id"] not in (None, decision_id)
    fresh = _decision(client, send["decision_id"])
    assert fresh["recommendation"]["action_preview"]["body"] == "Words nobody approved."
    assert fresh["status"] == "pending"
