"""Stream S1: SCIM 2.0 at org level (IDENTITY.md §4.9) and A72 (a SCIM key is good for nothing
but provisioning)."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    fresh_client,
    make_scim_key,
    make_user,
    sign_in,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import P, Scope, authn, permit

SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _provisioned(engine, pid: str, provider: str = "okta") -> str:
    """Mark a person as created by this SCIM connection: SCIM writes only to its own (F3/M2)."""
    with engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET created_by = :by WHERE id = :id"),
                   {"by": f"sys_scim_{provider}", "id": pid})
    return pid


def test_scim_creates_pending_people_and_finds_them_by_username(world, engine):
    key = make_scim_key(engine)
    client = fresh_client()
    res = client.post("/scim/v2/Users", headers=_bearer(key), json={
        "schemas": [SCIM_USER], "userName": "nia@example.test", "name": {"givenName": "Nia", "familyName": "Ode"},
        "emails": [{"value": "nia@example.test", "primary": True}], "active": True})
    assert res.status_code == 201, res.text
    assert res.headers["content-type"].startswith("application/scim+json")
    body = res.json()
    assert body["userName"] == "nia@example.test" and body["active"] is True
    with engine.begin() as db:
        status, pw = db.execute(text("SELECT p.status, u.password_hash FROM identity_principals p JOIN "
                                     "identity_users u ON u.principal_id = p.id WHERE p.id = :id"),
                                {"id": body["id"]}).first()
    assert status == "pending" and pw is None                      # until the first SSO sign-in
    found = client.get('/scim/v2/Users?filter=userName eq "nia@example.test"', headers=_bearer(key)).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == body["id"]
    assert client.get(f"/scim/v2/Users/{body['id']}", headers=_bearer(key)).status_code == 200
    assert client.post("/scim/v2/Users", headers=_bearer(key),
                       json={"userName": "nia@example.test"}).status_code == 409
    assert world.events_of("scim.user.created")


def test_scim_active_false_disables_with_the_cascade_and_true_reenables(world, engine):
    key = make_scim_key(engine)
    pid = _provisioned(engine, make_user(engine, "oli@example.test"))
    person = fresh_client()
    assert sign_in(person, "oli@example.test").status_code == 200
    scim_client = fresh_client()
    res = scim_client.patch(f"/scim/v2/Users/{pid}", headers=_bearer(key), json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": False}]})
    assert res.status_code == 200, res.text
    assert res.json()["active"] is False
    assert person.get("/v1/identity/me").status_code == 401            # sessions ended at once
    assert ("person_disabled", pid) in world.cascades
    assert world.events_of("scim.user.deactivated") and world.events_of("user.disabled")
    assert sign_in(fresh_client(), "oli@example.test").status_code == 403   # disabled
    res = scim_client.put(f"/scim/v2/Users/{pid}", headers=_bearer(key),
                          json={"userName": "oli@example.test", "active": True})
    assert res.status_code == 200 and res.json()["active"] is True
    assert sign_in(fresh_client(), "oli@example.test").status_code == 200   # had signed in: active


def test_scim_delete_disables_and_never_erases(world, engine):
    key = make_scim_key(engine)
    pid = _provisioned(engine, make_user(engine, "pia@example.test"))
    assert fresh_client().delete(f"/scim/v2/Users/{pid}", headers=_bearer(key)).status_code == 204
    with engine.begin() as db:
        assert db.execute(text("SELECT status FROM identity_principals WHERE id = :id"), {"id": pid}).scalar() == "disabled"


def test_scim_can_never_assign_access_roles(world, engine):
    key = make_scim_key(engine)
    client = fresh_client()
    res = client.post("/scim/v2/Users", headers=_bearer(key), json={
        "userName": "quin@example.test", "roles": [{"value": "owner"}]})
    assert res.status_code == 400 and res.json()["scimType"] == "invalidValue"
    pid = _provisioned(engine, make_user(engine, "rae@example.test", role="viewer"))
    for payload in ({"Operations": [{"op": "add", "path": "roles", "value": [{"value": "owner"}]}]},
                    {"Operations": [{"op": "replace", "value": {"entitlements": [{"value": "admin"}]}}]}):
        assert client.patch(f"/scim/v2/Users/{pid}", headers=_bearer(key), json=payload).status_code == 400
    with engine.begin() as db:
        held = {r[0] for r in db.execute(text("SELECT r.key FROM identity_role_bindings b JOIN identity_access_roles r "
                                              "ON r.id = b.role_id WHERE b.subject_id = :p"), {"p": pid})}
    assert held == {"viewer"}
    assert client.post("/scim/v2/Users", headers=_bearer(key),
                       json={"userName": "quin@example.test"}).status_code == 201


def test_scim_groups_are_read_only_scim_groups_with_members(world, engine):
    key = make_scim_key(engine)
    a = _provisioned(engine, make_user(engine, "sam@example.test"))
    b = _provisioned(engine, make_user(engine, "tia@example.test"))
    client = fresh_client()
    res = client.post("/scim/v2/Groups", headers=_bearer(key),
                      json={"displayName": "Builders", "externalId": "okta-1", "members": [{"value": a}]})
    assert res.status_code == 201, res.text
    gid = res.json()["id"]
    res = client.patch(f"/scim/v2/Groups/{gid}", headers=_bearer(key), json={"Operations": [
        {"op": "add", "path": "members", "value": [{"value": b}]},
        {"op": "remove", "path": f'members[value eq "{a}"]'}]})
    assert res.status_code == 200 and [m["value"] for m in res.json()["members"]] == [b]
    with engine.begin() as db:
        src = db.execute(text("SELECT g.source, m.source FROM identity_groups g JOIN identity_group_members m ON "
                              "m.group_id = g.id WHERE g.id = :g"), {"g": gid}).first()
    assert tuple(src) == ("scim", "scim")
    assert client.patch(f"/scim/v2/Groups/{gid}", headers=_bearer(key), json={"Operations": [
        {"op": "add", "path": "roles", "value": [{"value": "owner"}]}]}).status_code == 400
    assert client.delete(f"/scim/v2/Groups/{gid}", headers=_bearer(key)).status_code == 204
    assert client.get(f"/scim/v2/Groups/{gid}", headers=_bearer(key)).status_code == 404


def test_only_a_scim_key_provisions(world, engine):
    make_user(engine, "uma@example.test", role="owner")
    person = fresh_client()
    sign_in(person, "uma@example.test")
    assert person.get("/scim/v2/Users").status_code in (401, 403)       # an Owner's session: no
    assert fresh_client().get("/scim/v2/Users").status_code == 401      # nothing: no
    bad = authn.mint_api_key("scim")
    assert fresh_client().get("/scim/v2/Users", headers=_bearer(bad)).status_code == 401


def test_a72_a_scim_key_calling_role_bindings_is_403(world, engine, monkeypatch):
    key = make_scim_key(engine)
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    fixture = FastAPI(dependencies=[Depends(identity_guard.enforce_access)])
    identity_guard.install(fixture)

    @fixture.post("/v1/identity/role-bindings")
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.org())
    def bind():
        return {"bound": True}

    res = TestClient(fixture, base_url="https://boss.example.test").post(
        "/v1/identity/role-bindings", headers=_bearer(key), json={})
    assert res.status_code == 403 and res.json() == {"error": "forbidden", "permission": "role_binding:assign"}
    # nor can it mint itself a personal key
    res = fresh_client().post("/v1/identity/me/api-keys", headers=_bearer(key),
                              json={"name": "x", "scopes": ["scim:provision"]})
    assert res.status_code in (401, 403)


def test_round2_c_scim_group_writes_have_the_user_write_limits(world, engine):
    """Integration round 2 (c): a SCIM key may not move people it did not provision, nor an
    Owner/Admin, by group membership; and never adds anyone to a group an admin bound to Owner or
    Admin (that would place an Owner by IdP group)."""
    from boss_engine.identity import users as users_module
    from apps.api.tests.identity_s1_world import org_id

    key = make_scim_key(engine)
    mine = _provisioned(engine, make_user(engine, "own-scim@example.test"))
    theirs = make_user(engine, "invited@example.test")                 # invited in BOSS
    client = fresh_client()
    refused = client.post("/scim/v2/Groups", headers=_bearer(key),
                          json={"displayName": "Ops", "members": [{"value": theirs}]})
    assert refused.status_code == 403
    res = client.post("/scim/v2/Groups", headers=_bearer(key), json={"displayName": "Admins"})
    gid = res.json()["id"]
    with engine.begin() as db:
        users_module.add_binding(db, org_id=org_id(db), subject_id=gid, subject_type="group",
                                 role_key="admin", granted_by="test")
    added = client.patch(f"/scim/v2/Groups/{gid}", headers=_bearer(key), json={"Operations": [
        {"op": "add", "path": "members", "value": [{"value": mine}]}]})
    assert added.status_code == 403
    put = client.put(f"/scim/v2/Groups/{gid}", headers=_bearer(key),
                     json={"displayName": "Admins", "members": [{"value": mine}]})
    assert put.status_code == 403
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_group_members WHERE group_id = :g"),
                          {"g": gid}).scalar() == 0
