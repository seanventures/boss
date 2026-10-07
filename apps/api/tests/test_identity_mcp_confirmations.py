"""MCP confirmations under identity: decided by the PDP and the seats, never the legacy members
table (IDENTITY.md §6.13; FX-B). An MCP key asks; a separate person who holds the permission (and,
for an approval, the tier's seat) reviews the exact request in a session; the key replays it once.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.app import main as api_main
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import PrincipalKind
from boss_engine.mcp.api_surface import operation_alias
from boss_engine.services import mcp_confirmation

H = {"sec-fetch-site": "same-origin"}
TEMPLATE = "/v1/decisions/{decision_id}/approve"


@pytest.fixture
def world(engine, monkeypatch) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    # the transport middleware's identity path (the replay is consumed there)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(identity_guard, "presents_identity_credential", lambda conn: True)
    return w


@pytest.fixture
def cast(world):
    alice, carol = world.person("Alice"), world.person("Carol")
    for p in (alice, carol):
        world.bind(p, "operator", project=world.project_a)
    bot = world.principal("mcp-bot", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(bot, "operator", project=world.project_a)
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO identity_approval_seats (org_id, seat_key, principal_id, assigned_by) "
                        "VALUES (:o, 'ceo', :p, 'test')"), {"o": world.org, "p": alice.id})
    key = world.key_ctx(bot, ("decision:approve", "decision:read", "project:read"))
    return {"alice": alice, "carol": carol, "bot": bot, "key": key}


def _decision(world, tier=2):
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with world.engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, risk_tier, status, default_action) "
                        "VALUES (:d, :p, 'Ship it?', :t, 'pending', 'hold')"),
                   {"d": did, "p": world.project_a, "t": tier})
    return did


def _ask(world, ctx, did, body):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(ctx):
        return client.post("/v1/mcp-confirmations", headers=H, json={
            "project_id": world.project_a, "operation": operation_alias("POST", TEMPLATE),
            "method": "POST", "path": f"/v1/decisions/{did}/approve", "body": body})


def _review(world, ctx, cid, did, body):
    reviewed = {"project_id": world.project_a, "operation": operation_alias("POST", TEMPLATE),
                "method": "POST", "path": f"/v1/decisions/{did}/approve", "query": {}, "body": body}
    digest = mcp_confirmation.request_digest(**{k: reviewed[k] for k in
                                                ("project_id", "operation", "method", "path", "query", "body")})
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(ctx):
        return client.post(f"/v1/mcp-confirmations/{cid}/approve", headers=H, json={
            "project_id": world.project_a, "reviewed_digest": digest, "reviewed_request": reviewed})


def _replay(world, ctx, cid, did, body):
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(ctx):
        return client.post(f"/v1/decisions/{did}/approve", content=json.dumps(body), headers={
            **H, "content-type": "application/json", "Authorization": "Bearer boss_pat_test",
            "X-BOSS-MCP-Operation": operation_alias("POST", TEMPLATE),
            "X-BOSS-MCP-Confirmation": cid, "X-BOSS-MCP-Project": world.project_a})


def _status(world, did):
    with world.engine.connect() as db:
        return db.execute(text("SELECT status, approved_by FROM decisions WHERE id = :d"), {"d": did}).first()


def test_a_key_asks_a_seated_person_reviews_and_the_key_replays_once(world, cast):
    did = _decision(world)
    body = {"note": "reviewed by a person"}
    asked = _ask(world, cast["key"], did, body)
    assert asked.status_code == 201, asked.text
    cid = asked.json()["id"]
    no_seat = _review(world, world.ctx(cast["carol"]), cid, did, body)
    assert no_seat.status_code == 403 and "seat_required" in no_seat.text, no_seat.text
    stale = _review(world, world.ctx(cast["alice"], mfa=False), cid, did, body)
    assert stale.status_code == 401 and "step_up_required" in stale.text, stale.text
    approved = _review(world, world.ctx(cast["alice"]), cid, did, body)
    assert approved.status_code == 200, approved.text
    replay = _replay(world, cast["key"], cid, did, body)
    assert replay.status_code == 200, replay.text
    assert _status(world, did)[0] == "approved"
    again = _replay(world, cast["key"], cid, _decision(world), body)
    assert again.status_code in (403, 404, 409), again.text


def test_the_key_cannot_approve_without_a_confirmation(world, cast):
    did = _decision(world)
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(cast["key"]):
        out = client.post(f"/v1/decisions/{did}/approve", json={"note": "x"}, headers=H)
    assert out.status_code in (401, 403), out.text
    assert _status(world, did)[0] == "pending"


def test_asking_needs_the_operations_permission_and_reviewing_needs_a_separate_person(world, cast):
    viewer_bot = world.principal("viewer-bot", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(viewer_bot, "viewer", project=world.project_a)
    weak = world.key_ctx(viewer_bot, ("decision:read", "project:read"))
    did = _decision(world, tier=1)
    refused = _ask(world, weak, did, {"note": "x"})
    assert refused.status_code == 403 and "decision:approve" in refused.text, refused.text
    person = _ask(world, world.ctx(cast["alice"]), did, {"note": "x"})
    assert person.status_code == 409 and "confirm_it_yourself" in person.text
    asked = _ask(world, cast["key"], did, {"note": "x"})
    cid = asked.json()["id"]
    by_key = _review(world, cast["key"], cid, did, {"note": "x"})
    # a key never reviews: refused at the route (no step-up) or by the reviewer rule
    assert by_key.status_code in (401, 403), by_key.text
    assert _status(world, did)[0] == "pending"


def test_a_reviewer_demoted_after_approving_voids_the_replay(world, cast):
    did = _decision(world, tier=1)
    body = {"note": "x"}
    cid = _ask(world, cast["key"], did, body).json()["id"]
    assert _review(world, world.ctx(cast["carol"]), cid, did, body).status_code == 200
    with world.engine.begin() as db:
        db.execute(text("DELETE FROM identity_role_bindings WHERE subject_id = :p"), {"p": cast["carol"].id})
        from boss_engine.identity import authz

        authz.bump_epoch(world.org, db=db, reason="test")
    out = _replay(world, cast["key"], cid, did, body)
    assert out.status_code == 403 and "reviewer_no_longer_allowed" in out.text, out.text
    assert _status(world, did)[0] == "pending"


def test_no_confirmation_path_reads_the_legacy_members_table_under_enforce():
    import ast
    import inspect

    from apps.api.app import identity_mcp_confirmations

    source = inspect.getsource(identity_mcp_confirmations)
    for name in ("project_members", "org_members", "require_member", "members."):
        assert name not in source, name
    fn = ast.parse(inspect.getsource(api_main._identity_request_mcp_confirmation))
    assert "require_member" not in ast.unparse(fn) and "_require_exclusive_mcp_project" not in ast.unparse(fn)
