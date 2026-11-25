"""A team of 20 provisioned by SCIM: roles follow groups, removal ends access at once (BOSSAPP-1294).

The preview team signs in through Okta and Okta pushes people and groups over SCIM. This test does
that for 20 people and three groups, then checks what an operator would check:

- every person's roles are exactly the roles bound to the groups they are in (``users.role_keys``);
- a person in the mapped group is served a route that needs the role's permission, a person not
  in it is refused, through real sign-in sessions and the REAL PDP in enforce mode;
- moving a person between groups over SCIM changes their access on the very next request;
- ``active=false`` and DELETE end the person's live session at once, and nobody else's.

People are created over SCIM (``POST /scim/v2/Users``). SCIM creates them ``pending`` with no
password (they sign in through the IdP). To get a session in a test, each is then given a password
and marked active, which stands in for their first SSO sign-in; SSO sign-in itself is covered by
test_identity_authn_sso.py and test_identity_r2_scim_people_sign_in_by_sso.py.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    STRONG,
    csrf,
    fresh_client,
    make_scim_key,
    world,
)
from apps.api.tests.test_identity_fxd_idp_groups import (  # noqa: F401  (real_pdp is a fixture)
    PATCH_OP,
    ROUTE,
    SCIM_GROUP,
    _refused,
    _signed_in,
    real_pdp,
)
from boss_engine.identity import signin, users
from boss_engine.identity.passwords import hash_password

SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
TEAM = 20
#: Fictional Okta groups → the BOSS role an admin binds to each. "readers" holds group:read (the
#: permission ROUTE needs); "operators" is the built-in Operator role; "observers" is bound to
#: nothing, so its members get no role from it.
PLATFORM, DEVELOPERS, OBSERVERS = "gw-platform", "gw-developers", "gw-observers"


def _email(i: int) -> str:
    return f"dev{i:02d}@example.test"


def _provision(scim: dict, i: int) -> str:
    res = fresh_client().post("/scim/v2/Users", headers=scim, json={
        "schemas": [SCIM_USER], "userName": _email(i), "externalId": f"okta-{i:02d}",
        "name": {"givenName": "Dev", "familyName": f"{i:02d}"},
        "emails": [{"value": _email(i), "primary": True}], "active": True})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _first_sign_in(engine, pids: list[str]) -> None:
    """Stand-in for each person's first SSO sign-in: active, verified, with a password."""
    with engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'active' WHERE id = ANY(:ids)"),
                   {"ids": pids})
        db.execute(text("UPDATE identity_users SET password_hash = :h, password_changed_at = now(), "
                        "email_verified_at = now() WHERE principal_id = ANY(:ids)"),
                   {"h": hash_password(STRONG), "ids": pids})


def _group(scim: dict, name: str, members: list[str]) -> str:
    res = fresh_client().post("/scim/v2/Groups", headers=scim, json={
        "schemas": [SCIM_GROUP], "displayName": name, "members": [{"value": m} for m in members]})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _role(owner, key: str, permissions: list[str]) -> str:
    made = owner.post("/v1/identity/access-roles", headers=csrf(owner), json={
        "key": key, "name": key, "permissions": permissions, "bindable_scopes": ["org"]})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _bind(owner, group_id: str, role_id: str) -> None:
    res = owner.post("/v1/identity/role-bindings", headers=csrf(owner), json={
        "subject_type": "group", "subject_id": group_id, "role_id": role_id})
    assert res.status_code == 201, res.text


def _roles(engine, pid: str) -> set[str]:
    with engine.connect() as db:
        return set(users.role_keys(db, pid))


def test_a_team_of_twenty_gets_roles_from_okta_groups_and_loses_access_at_once(real_pdp, engine,
                                                                               monkeypatch):
    # 21 sign-ins from the test client's one address would trip the per-IP sign-in limit
    # (20/min, signin.ip_limited); a real team signs in from its own machines.
    monkeypatch.setattr(signin, "ip_limited", lambda db, facts: None)
    key = make_scim_key(engine)                      # also an Owner: scim-owner-okta@example.test
    scim = {"Authorization": f"Bearer {key}"}
    owner = _signed_in(engine, "scim-owner-okta@example.test")

    pids = [_provision(scim, i) for i in range(TEAM)]
    listed = fresh_client().get("/scim/v2/Users?count=200", headers=scim).json()
    assert set(pids) <= {r["id"] for r in listed["Resources"]}
    _first_sign_in(engine, pids)
    assert all(_roles(engine, p) == set() for p in pids)          # SCIM people start with no role

    platform, developers, observers = pids[:5], pids[5:17], pids[17:]
    g_platform = _group(scim, PLATFORM, platform)
    g_developers = _group(scim, DEVELOPERS, developers)
    g_observers = _group(scim, OBSERVERS, observers)
    readers = f"gw_readers_{uuid.uuid4().hex[:6]}"
    _bind(owner, g_platform, _role(owner, readers, ["group:read"]))
    _bind(owner, g_developers, "arl_operator")
    del g_observers                                               # bound to nothing on purpose

    expected = {**{p: {readers} for p in platform}, **{p: {"operator"} for p in developers},
                **{p: set() for p in observers}}
    assert {p: _roles(engine, p) for p in pids} == expected

    sessions = {p: _signed_in(engine, _email(i)) for i, p in enumerate(pids)}
    for p in platform:
        assert sessions[p].get(ROUTE).status_code == 200
    for p in observers:
        assert _refused(sessions[p].get(ROUTE))

    # Okta moves one platform engineer to the observers: the next request is refused.
    moved = platform[0]
    res = fresh_client().patch(f"/scim/v2/Groups/{g_platform}", headers=scim, json={
        "schemas": [PATCH_OP],
        "Operations": [{"op": "remove", "path": f'members[value eq "{moved}"]'}]})
    assert res.status_code == 200, res.text
    assert _roles(engine, moved) == set()
    assert _refused(sessions[moved].get(ROUTE))
    assert sessions[platform[1]].get(ROUTE).status_code == 200    # the rest of the group keeps it

    # Okta deactivates one person and deletes another: both sessions end at once.
    off, gone = platform[1], developers[0]
    res = fresh_client().patch(f"/scim/v2/Users/{off}", headers=scim, json={
        "schemas": [PATCH_OP], "Operations": [{"op": "replace", "path": "active", "value": False}]})
    assert res.status_code == 200, res.text
    assert fresh_client().delete(f"/scim/v2/Users/{gone}", headers=scim).status_code == 204
    for p in (off, gone):
        assert sessions[p].get("/v1/identity/me").status_code == 401
    with engine.connect() as db:
        live = db.execute(text("SELECT count(*) FROM identity_sessions WHERE principal_id = ANY(:ids) "
                               "AND revoked_at IS NULL"), {"ids": [off, gone]}).scalar()
    assert live == 0
    still = [p for p in pids if p not in (off, gone)]
    assert all(sessions[p].get("/v1/identity/me").status_code == 200 for p in still)
    assert sessions[platform[2]].get(ROUTE).status_code == 200
