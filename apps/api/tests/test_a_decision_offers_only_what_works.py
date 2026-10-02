"""A decision offers only the answers that do something — through the real endpoints.

PORTED AT INTEGRATION. The console-a stream wrote these in the private repo's
apps/api/tests/test_api.py, a file the Community tree does not have, so the endpoint half of its
decision action contract (services/decision_actions.py) would have arrived here untested.

THE INCIDENTS. A pull-request review offered "Request changes"; nothing read
`revision_requested` for it, and that status refused a later approval, so the PR could never be
merged from BOSS. Rows parked that way exist on real installs and must still be answerable.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text


def _general_decision(engine, risk_tier: int = 2, *, action: str = "governance_review",
                      status: str = "pending") -> str:
    """A decision of the general kind: approve, reject and delegate apply. Revise applies only
    to an action whose follow-on reads the note (decision_actions.REVISE_CONSUMED)."""
    decision_id = f"dec_general_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
                "default_action, status) VALUES (:i, 'boss-dogfood', 'Adopt the plan?', "
                "CAST(:rec AS JSONB), :t, 'hold', :s)"
            ),
            {"i": decision_id, "t": risk_tier, "s": status,
             "rec": json.dumps({"action": action})},
        )
    return decision_id


def test_request_changes_is_refused_where_nothing_reads_it_and_says_what_to_do(client, engine):
    """A PR review offered "Request changes"; nothing read revision_requested for it, and the
    status refused a later approval, so the PR could never be merged from BOSS."""
    decision_id = _general_decision(engine, action="review_pull_request")
    revise = client.post(f"/v1/decisions/{decision_id}/revise",
                         json={"actor": "ceo", "note": "Rename the table."})
    assert revise.status_code == 409, revise.text
    assert "Factory" in revise.json()["detail"]
    with engine.begin() as connection:
        assert connection.execute(text("SELECT status FROM decisions WHERE id = :i"),
                                  {"i": decision_id}).scalar_one() == "pending"


def test_a_decision_stuck_in_a_dead_end_revision_can_be_answered_again(client, engine):
    """Rows parked by the old "Request changes" exist on real installs. They reopen when
    answered, and the reopening is recorded."""
    decision_id = _general_decision(engine, status="revision_requested")
    reject = client.post(f"/v1/decisions/{decision_id}/reject",
                         json={"actor": "ceo", "note": "Not doing this."})
    assert reject.status_code == 200, reject.text
    assert reject.json()["status"] == "rejected"
    with engine.begin() as connection:
        was = connection.execute(text(
            "SELECT data->>'was' FROM events WHERE entity_id = :i "
            "AND event_type = 'decision.reopened'"), {"i": decision_id}).scalar_one()
    assert was == "revision_requested"


def test_a_general_decision_can_still_be_delegated_and_approved(client, engine):
    """The transitions themselves still work where they apply (the private suite's delegate
    test, on a decision that offers them rather than a spending refusal, whose only answer is
    now Dismiss)."""
    decision_id = _general_decision(engine)
    delegate = client.post(
        f"/v1/decisions/{decision_id}/delegate",
        json={"actor": "ceo", "delegate_to": "release_manager",
              "note": "Release manager should own this final call."},
    )
    assert delegate.status_code == 200, delegate.text
    assert delegate.json()["status"] == "delegated"
    assert delegate.json()["delegated_to"] == "release_manager"
    approve = client.post(f"/v1/decisions/{decision_id}/approve",
                          json={"actor": "release_manager",
                                "note": "Approved after delegated review."})
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "approved"
    assert approve.json()["approved_by"] == "release_manager"
