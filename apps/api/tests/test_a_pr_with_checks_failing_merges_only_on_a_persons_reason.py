"""A pull request that opened with its checks failing merges only on a person's stated reason.

The build's checks still failed after its repair rounds, so its PR opened anyway (a draft,
labelled `checks failing`, services/checks_failing.py) and the workflow went on to the merge
decision. That card says what is failing, security first, and must never merge silently: a plain
Approve is refused with the failing checks named, and a person's forced merge with a note is
the decision — recorded with who, why and which checks.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import checks_failing as cf

PROJECT = "boss-dogfood"
SSRF = ("## Findings\n- HIGH: POST /api/fetch-preview fetches any URL the caller gives it "
        "(SSRF) in server/preview.ts:22\n\nVERDICT: ISSUES")


def _merge_card(engine) -> str:
    decision_id = f"dec_cf_{uuid4().hex[:10]}"
    record = {**cf.collect(["security_clean", "written_tests_pass"], {"security_summary": SSRF}),
              "draft": True, "label": cf.LABEL}
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
            "default_action, status) VALUES (:i, :p, 'Checks failing — decide the merge: T', "
            "CAST(:r AS JSONB), 1, 'hold', 'pending')"),
            {"i": decision_id, "p": PROJECT, "r": json.dumps({
                "action": "review_pull_request", cf.KEY: record,
                "pull_request_url": f"https://github.com/acme/web/pull/{uuid4().int % 10000}"})})
    return decision_id


def test_a_plain_approve_is_refused_naming_the_failing_checks(client, engine):
    decision_id = _merge_card(engine)
    refused = client.post(f"/v1/decisions/{decision_id}/approve",
                          json={"actor": "ceo", "note": "looks fine"})
    assert refused.status_code == 409, refused.text
    assert "Checks are failing" in refused.text and "Security clean" in refused.text
    assert "security finding" in refused.text
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :i"),
                         {"i": decision_id}).scalar_one() == "pending"


def test_a_forced_merge_without_a_reason_is_refused(client, engine):
    decision_id = _merge_card(engine)
    refused = client.post(f"/v1/decisions/{decision_id}/approve",
                          json={"actor": "ceo", "note": "", "force": True})
    assert refused.status_code == 409, refused.text
    assert "note" in refused.text


def test_a_persons_merge_with_a_reason_is_the_decision_and_is_recorded(client, engine):
    decision_id = _merge_card(engine)
    reason = "SSRF is behind the admin VPN for this release; the follow-up fixes it"
    approved = client.post(f"/v1/decisions/{decision_id}/approve",
                           json={"actor": "ceo", "note": reason, "force": True})
    assert approved.status_code == 200, approved.text
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT data FROM events WHERE entity_id = :i AND event_type = :t "
            "ORDER BY created_at DESC LIMIT 1"),
            {"i": decision_id, "t": cf.MERGE_EVENT}).mappings().first()
    assert row is not None, "the merge with failing checks is recorded"
    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    assert data["reason"] == reason
    assert data["failing_gates"] == ["security_clean", "written_tests_pass"]
    assert data["security"] is True


def test_the_card_offers_merge_with_checks_failing_with_a_reason(client, engine):
    decision_id = _merge_card(engine)
    got = client.get(f"/v1/decisions/{decision_id}")
    assert got.status_code == 200, got.text
    card = got.json()
    approve = next(a for a in card["allowed_actions"] if a["action"] == "approve")
    assert approve["label"] == "Merge with checks failing" and approve["needs"] == "note"
    assert card["recommendation"][cf.KEY]["gates"][0]["gate"] == "security_clean"
