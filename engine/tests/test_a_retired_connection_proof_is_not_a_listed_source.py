"""A retired connection proof is not a source anybody connected (BOSSAPP-539).

Every Add model → Connect stages a temporary proof-<hash> entitlement source for its one paid
proof call and retires it (status inactive) afterwards. Repo and models listed those retired
rows as if they were real, with Replace key and Disconnect session on them. They stay in the
audit trail, never deleted; the list leaves them out; their history names them by what they
proved: provider, outcome, and the event's own time.

Real authority on a disposable Postgres; only provider replies are scripted.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.services import setup_checks
from boss_engine.services.activity_log import _build_event
from boss_engine.services.entitlements import EntitlementBrokerService

from .test_connection_governance import database, proof, scope  # noqa: F401  (fixtures)
from .test_connection_proof_outcome import KEY, anthropic_error, anthropic_proof


def listed(scope):  # noqa: F811
    e, p, s = scope
    with e.connect() as c:
        return [x["source_id"] for x in EntitlementBrokerService(s).list_sources(c, p)]


def proof_rows(e, p):
    with e.connect() as c:
        return c.execute(text(
            "SELECT source_id, status, connection_status FROM entitlement_sources "
            "WHERE project_id=:p AND source_id LIKE 'proof-%'"), {"p": p}).mappings().all()


def retired_events(e, p):
    with e.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, event_type, actor, entity_type, entity_id, data, created_at FROM events "
            "WHERE project_id=:p AND event_type='connection.proof_retired' ORDER BY created_at"),
            {"p": p}).mappings()]


def test_an_inactive_proof_source_never_appears_in_the_sources_list(scope):  # noqa: F811
    e, p, s = scope
    seen_during_the_call = []

    def contact(binding):
        # While its one call is in flight the proof source is active: the router needs it.
        seen_during_the_call.extend(x for x in listed(scope) if x.startswith("proof-"))
        return {"ok": True, "state": "working", "cost_usd_micros": 1000}

    assert proof(scope, callback=contact)["ok"] is True
    assert len(seen_during_the_call) == 1
    # Retired, kept for the audit trail, never deleted...
    rows = proof_rows(e, p)
    assert [(r["status"], r["connection_status"]) for r in rows] == [("inactive", "revoked")]
    # ...and never listed as a source again.
    assert not [x for x in listed(scope) if x.startswith("proof-")]
    # A lookup by id (receipts and leases point at it) still finds it.
    with e.connect() as c:
        found = EntitlementBrokerService(s).get_source(c, p, rows[0]["source_id"])
    assert found["status"] == "inactive"


def test_every_retired_proof_is_left_out_however_many_connects_there_were(scope):  # noqa: F811
    e, p, _ = scope
    for n in range(3):
        assert proof(scope, intent=f"connect-{n}")["ok"] is True
    assert len(proof_rows(e, p)) == 3
    assert not [x for x in listed(scope) if x.startswith("proof-")]


def test_a_proof_that_worked_is_named_by_provider_and_outcome(scope):  # noqa: F811
    e, p, _ = scope
    ok = setup_checks.HttpAnswer(200, {}, {"usage": {"input_tokens": 10, "output_tokens": 2}})
    assert anthropic_proof(scope, "claude-sonnet-5", ok)["ok"] is True
    [event] = retired_events(e, p)
    data = event["data"]
    assert data["name"] == "Anthropic connection proof"
    assert data["provider"] == "anthropic" and data["model"] == "claude-sonnet-5"
    assert data["outcome"] == "replied_ok"
    assert data["summary"] == "Anthropic replied OK with claude-sonnet-5."
    assert data["receipt_id"] and data["source_id"].startswith("proof-")
    assert KEY not in json.dumps(data)


def test_a_refused_proof_is_named_by_what_the_provider_said(scope):  # noqa: F811
    e, p, _ = scope
    refused = anthropic_error(404, "not_found_error", "model: claude-sonnet-9")
    assert anthropic_proof(scope, "claude-sonnet-9", refused)["ok"] is False
    [event] = retired_events(e, p)
    assert event["data"]["outcome"] == "refused"
    assert event["data"]["summary"] == "Anthropic refused: model not found (claude-sonnet-9)."
    assert not [x for x in listed(scope) if x.startswith("proof-")]


def test_a_proof_stopped_before_the_call_says_it_was_not_sent(scope):  # noqa: F811
    e, p, _ = scope

    def expire():
        with e.begin() as c:
            c.execute(text("UPDATE jobs SET lease_expires_at=now()-interval '1 second' "
                           "WHERE project_id=:p"), {"p": p})

    result = proof(scope, before=expire, callback=lambda _: (_ for _ in ()).throw(
        AssertionError("expired authority contacted")))
    assert result["ok"] is False
    [event] = retired_events(e, p)
    assert event["data"]["outcome"] == "not_sent"
    assert event["data"]["summary"].startswith("OpenAI was not asked: ")
    assert not [x for x in listed(scope) if x.startswith("proof-")]


def test_a_proof_is_retired_once_with_one_history_line(scope):  # noqa: F811
    e, p, _ = scope
    assert proof(scope)["ok"] is True
    assert proof(scope)["ok"] is True  # the same intent replays; nothing is retired twice
    assert len(retired_events(e, p)) == 1


def test_the_retired_proof_is_named_by_what_it_proved_not_its_proof_id(scope):  # noqa: F811
    e, p, _ = scope
    refused = anthropic_error(401, "authentication_error", f"invalid x-api-key {KEY}")
    anthropic_proof(scope, "claude-sonnet-5", refused)
    [event] = retired_events(e, p)
    item = _build_event({"id": event["id"], "ts": event["created_at"], "raw": {
        "event_type": event["event_type"], "actor": event["actor"],
        "entity_type": event["entity_type"], "entity_id": event["entity_id"],
        "data": event["data"]}})
    assert item["data"]["name"] == "Anthropic connection proof"
    assert item["data"]["summary"] == "Anthropic refused: the key was not accepted."
    assert "proof-" not in item["title"] + item["data"]["name"] + item["data"]["summary"]
    assert KEY not in json.dumps(item)
