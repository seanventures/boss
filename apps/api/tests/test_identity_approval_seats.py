"""Approvals and seats: separation of duties, over HTTP (IDENTITY.md §6.13; FX-B P0-7).

All four conditions, against the real app under enforce:
  1. decision:approve (the declaration) — a Viewer is refused;
  2. the seat the tier needs — an Operator with no seat is refused a tier-2 approval and allowed a
     tier-1 one; the ceo-seat holder approves tier 2; a VACANT seat is held by the Owners;
  3. a human session — the same seat-holder with an API key, and an agent run, are refused;
  4. tier 3 needs two distinct people (202 then 200), the same person twice is 409, and the
     single-operator waiver lets one person approve.
Plus PUT /v1/identity/seats/{seat_key}: Owner only, people only, never empties a tier.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld

H = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    return w


@pytest.fixture
def people(world):
    owner, alice, bob, carol, viewer = (world.person(n) for n in ("Owner", "Alice", "Bob", "Carol", "Vic"))
    world.bind(owner, "owner")
    for p in (alice, bob, carol):
        world.bind(p, "operator", project=world.project_a)
    world.bind(viewer, "viewer", project=world.project_a)
    with world.engine.begin() as db:
        for seat, p in (("ceo", alice), ("cao", bob)):
            db.execute(text("INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                            "VALUES (:o, :s, :p, 'test')"), {"o": world.org, "s": seat, "p": p.id})
    return {"owner": owner, "alice": alice, "bob": bob, "carol": carol, "viewer": viewer}


def _decision(world, tier: int) -> str:
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, risk_tier, status, default_action) "
                        "VALUES (:d, :p, 'Ship it?', :t, 'pending', 'hold')"),
                   {"d": did, "p": world.project_a, "t": tier})
    return did


def _approve(world, ctx, did):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(ctx):
        return client.post(f"/v1/decisions/{did}/approve", json={"actor": "x", "note": "ok"}, headers=H)


def _status(world, did):
    with world.engine.connect() as db:
        return db.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": did}).scalar()


def test_1_a_viewer_cannot_approve(world, people):
    out = _approve(world, world.ctx(people["viewer"]), _decision(world, 1))
    assert out.status_code == 403 and "decision:approve" in out.text


def test_2_a_tier_2_decision_needs_the_ceo_seat(world, people):
    did = _decision(world, 2)
    refused = _approve(world, world.ctx(people["carol"]), did)
    assert refused.status_code == 403 and refused.json().get("error") == "seat_required", refused.text
    assert refused.json().get("seats") == ["ceo"] and _status(world, did) == "pending"
    allowed = _approve(world, world.ctx(people["alice"]), did)
    assert allowed.status_code == 200, allowed.text
    assert _status(world, did) == "approved"


def test_2_a_tier_1_decision_needs_no_seat(world, people):
    did = _decision(world, 1)
    assert _approve(world, world.ctx(people["carol"]), did).status_code == 200
    assert _status(world, did) == "approved"


def test_2_a_vacant_seat_is_held_by_the_owners(world, people):
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_approval_seats WHERE org_id = :o"), {"o": world.org})
    did = _decision(world, 2)
    assert _approve(world, world.ctx(people["carol"]), did).status_code == 403
    out = _approve(world, world.ctx(people["owner"]), did)
    assert out.status_code == 200, out.text


def test_3_an_api_key_or_an_agent_run_cannot_approve(world, people):
    did = _decision(world, 1)
    key = world.key_ctx(people["alice"], ("decision:approve", "decision:read", "project:read"))
    by_key = _approve(world, key, did)
    assert by_key.status_code in (401, 403), by_key.text
    agent = world.agent("approver-agent", ceiling=("decision:read", "project:read"), sponsor=people["alice"])
    run = world.run_ctx(people["alice"], (agent,), grant_permissions=("decision:read", "project:read"),
                        token_scope=("decision:read", "project:read"), project_id=world.project_a)
    by_agent = _approve(world, run, did)
    assert by_agent.status_code == 403, by_agent.text
    assert _status(world, did) == "pending"


def test_3_a_key_that_passes_the_route_is_refused_at_the_approval(world, people, monkeypatch):
    """Even if the PDP let a key through (a bridge, a future flag), the approval itself asks for a
    person: condition 3 is checked where the approval happens, not only by step-up."""
    from boss_engine.identity import approvals

    did = _decision(world, 1)
    key = world.key_ctx(people["alice"], ("decision:approve", "decision:read", "project:read"))
    with world.engine.begin() as db, pytest.raises(approvals.ApprovalRefused) as refused:
        approvals.require_approval(db, key, {"id": did, "risk_tier": 1})
    assert refused.value.code == "human_session_required"


def test_4_tier_3_needs_two_distinct_people(world, people):
    did = _decision(world, 3)
    first = _approve(world, world.ctx(people["alice"]), did)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "awaiting_second_approval" and first.json()["needed"] == 2
    assert _status(world, did) == "pending"
    again = _approve(world, world.ctx(people["alice"]), did)
    assert again.status_code == 409 and again.json()["error"] == "second_approver_must_differ"
    no_seat = _approve(world, world.ctx(people["carol"]), did)
    assert no_seat.status_code == 403 and no_seat.json()["error"] == "seat_required"
    second = _approve(world, world.ctx(people["bob"]), did)
    assert second.status_code == 200, second.text
    assert _status(world, did) == "approved"
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["carol"])):
        seen = client.get(f"/v1/identity/decisions/{did}/approvals").json()
    assert [a["principal_id"] for a in seen["approvals"]] == [people["alice"].id, people["bob"].id]
    assert seen["needed"] == 2


def test_4_the_single_operator_waiver_lets_one_person_approve_tier_3(world, people):
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_settings (org_id, policy, epoch) VALUES (:o, CAST(:p AS jsonb), 1) "
                        "ON CONFLICT (org_id) DO UPDATE SET policy = EXCLUDED.policy, "
                        "epoch = identity_settings.epoch + 1"),
                   {"o": world.org, "p": json.dumps({"single_operator_waiver": True})})
    did = _decision(world, 3)
    out = _approve(world, world.ctx(people["alice"]), did)
    assert out.status_code == 200, out.text
    assert _status(world, did) == "approved"


def test_4_the_waiver_is_automatic_only_with_one_active_person(engine):
    from boss_engine.identity import approvals

    world = IdentityWorld.create(engine)
    solo = world.person("Solo")
    with engine.begin() as db:
        assert approvals.waiver(db, world.org) == {"effective": True, "setting": None, "active_people": 1}
    world.person("Second")
    with engine.begin() as db:
        assert approvals.waiver(db, world.org)["effective"] is False
    assert solo


# ---- PUT /v1/identity/seats/{seat_key} --------------------------------------------------------------

def _put(world, ctx, seat, ids):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(ctx):
        return client.put(f"/v1/identity/seats/{seat}", json={"principal_ids": ids}, headers=H)


def test_only_an_owner_assigns_seats(world, people):
    admin = world.person("Adm")
    world.bind(admin, "admin")
    refused = _put(world, world.ctx(admin), "ceo", [people["carol"].id])
    assert refused.status_code == 403 and "approval_rule:manage" in refused.text
    out = _put(world, world.ctx(people["owner"]), "ceo", [people["carol"].id])
    assert out.status_code == 200, out.text
    assert [h["principal_id"] for h in out.json()["holders"]] == [people["carol"].id]
    did = _decision(world, 2)
    assert _approve(world, world.ctx(people["alice"]), did).status_code == 403      # moved away
    assert _approve(world, world.ctx(people["carol"]), did).status_code == 200


def test_a_seat_is_held_by_people_and_never_empties_a_tier(world, people):
    agent = world.agent("seat-agent")
    bad = _put(world, world.ctx(people["owner"]), "ceo", [agent.id])
    assert bad.status_code == 422 and bad.json()["error"] == "not_a_person"
    unknown = _put(world, world.ctx(people["owner"]), "emperor", [people["alice"].id])
    assert unknown.status_code == 422 and unknown.json()["error"] == "unknown_seat"
    empty = _put(world, world.ctx(people["owner"]), "ceo", [])
    assert empty.status_code == 409 and empty.json()["error"] == "seat_required"
    # cao can be emptied: tier 3 still has ceo
    assert _put(world, world.ctx(people["owner"]), "cao", []).status_code == 200


def test_the_seats_listing_says_how_tiers_map(world, people):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(people["owner"])):
        body = client.get("/v1/identity/seats").json()
    assert body["tiers"]["2"] == ["ceo"] and body["two_person_tiers"] == [3]
    assert any(s["seat_key"] == "ceo" and s["principal_id"] == people["alice"].id for s in body["seats"])
    assert body["single_operator_waiver"]["active_people"] >= 5


def test_the_first_owner_is_seated_at_the_claim(engine):
    from boss_engine.identity import approvals

    world = IdentityWorld.create(engine)
    owner = world.person("First")
    with engine.begin() as db:
        approvals.seat_the_first_owner(db, world.org, owner.id)
        seated = {r["seat_key"] for r in approvals.seat_rows(db, world.org)}
    assert {"ceo", "cao"} <= seated
