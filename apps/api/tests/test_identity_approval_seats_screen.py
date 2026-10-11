"""Identity › Approval seats, over HTTP: what the screen reads and writes, and the refusal that
points at it (IDENTITY.md §6.13).

  * GET /v1/identity/seats gives each seat's holders and the tiers it approves;
  * an Owner adds and removes a holder with PUT (the screen's Add / Remove), an Admin cannot;
  * a person without the seat is refused with the seat's CURRENT holders named and a link to
    /identity/seats, not a bare "ask an Admin"; a vacant seat says an Owner approves.
"""
from __future__ import annotations

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
    owner, alice, carol, admin = (world.person(n) for n in ("Owner", "Alice", "Carol", "Adm"))
    world.bind(owner, "owner")
    world.bind(admin, "admin")
    for p in (alice, carol):
        world.bind(p, "operator", project=world.project_a)
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                        "VALUES (:o, 'ceo', :p, 'test')"), {"o": world.org, "p": alice.id})
    return {"owner": owner, "alice": alice, "carol": carol, "admin": admin}


def _client():
    return TestClient(app, raise_server_exceptions=False)


def _seats(world, ctx):
    with world.acting_as(ctx):
        return _client().get("/v1/identity/seats", headers=H)


def _put(world, ctx, seat, ids):
    with world.acting_as(ctx):
        return _client().put(f"/v1/identity/seats/{seat}", json={"principal_ids": ids}, headers=H)


def test_the_screen_reads_each_seat_its_holders_and_its_tiers(world, people):
    out = _seats(world, world.ctx(people["owner"]))
    assert out.status_code == 200, out.text
    body = out.json()
    assert [(r["seat_key"], r["principal_id"], r["display_name"]) for r in body["seats"]] == [
        ("ceo", people["alice"].id, "Alice")]
    assert "ceo" in body["tiers"]["2"] and "ceo" in body["known_seats"]
    assert 3 in body["two_person_tiers"]


def test_an_owner_adds_and_removes_a_holder_and_an_admin_cannot(world, people):
    alice, carol = people["alice"].id, people["carol"].id
    refused = _put(world, world.ctx(people["admin"]), "ceo", [alice, carol])
    assert refused.status_code == 403 and "approval_rule:manage" in refused.text
    added = _put(world, world.ctx(people["owner"]), "ceo", [alice, carol])
    assert added.status_code == 200, added.text
    assert sorted(h["principal_id"] for h in added.json()["holders"]) == sorted([alice, carol])
    removed = _put(world, world.ctx(people["owner"]), "ceo", [carol])
    assert removed.status_code == 200, removed.text
    assert [h["principal_id"] for h in removed.json()["holders"]] == [carol]


def test_the_refusal_names_the_holders_and_links_to_the_seats_screen(world, people):
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, risk_tier, status, default_action) "
                        "VALUES (:d, :p, 'Ship it?', 2, 'pending', 'hold')"), {"d": did, "p": world.project_a})
    with world.acting_as(world.ctx(people["carol"])):
        out = _client().post(f"/v1/decisions/{did}/approve", json={"actor": "x", "note": "ok"}, headers=H)
    assert out.status_code == 403 and out.json()["error"] == "seat_required", out.text
    body = out.json()
    assert "Alice" in body["message"] and "Approval seats" in body["message"]
    assert "ask an Admin" not in body["message"]
    assert body["href"] == "/identity/seats" and body["href_label"] == "Approval seats"
    assert [h["principal_id"] for h in body["holders"]] == [people["alice"].id]


def test_a_vacant_seat_refusal_says_an_owner_approves(world, people):
    from boss_engine.identity import approvals

    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_approval_seats WHERE org_id = :o"), {"o": world.org})
        refusal = approvals.seat_refusal(db, world.org, 2, ("ceo",))
    assert refusal.extra["holders"] == [] and "an Owner approves it" in refusal.message
