"""IdP-sourced groups grant access, and stop granting it, over HTTP (IDENTITY.md §6.8, §4.9;
FX-D P1-21 items 3a, 11a).

The existing SCIM and SSO tests prove membership rows appear and disappear. These prove the ACCESS
does: an Owner maps the synced group to a role in BOSS, the member's next request needing that
role's permission is served, and when the IdP removes the member (SCIM PATCH / DELETE, or an OIDC
sign-in whose groups claim no longer lists the group) the member's very next request is refused —
through real sign-in sessions, the real enforcement point (enforce mode) and the REAL PDP (the S1
world's fake authz is put back to the real one here).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    csrf,
    fresh_client,
    make_scim_key,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from apps.api.tests.test_identity_authn_sso import (  # noqa: F401  (idp is a fixture)
    assert_signed_in,
    idp,
    invite,
    login_via,
    make_provider,
)
from boss_engine.config import get_settings
from boss_engine.identity import authn, authz

#: The real PDP, captured at import (before any fixture swaps in the S1 fakes).
REAL_PDP = {name: getattr(authz, name) for name in ("check", "effective_permissions", "bump_epoch")}
SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
#: An org route Viewer does not hold (OAU): the permission the mapped group's role grants.
ROUTE, PERMISSION = "/v1/identity/groups", "group:read"


@pytest.fixture
def real_pdp(world, monkeypatch):
    for name, fn in REAL_PDP.items():
        monkeypatch.setattr(authz, name, fn)
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    return world


def _signed_in(engine, email):
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def _group_reader_role(owner):
    made = owner.post("/v1/identity/access-roles", headers=csrf(owner), json={
        "key": f"group_readers_{uuid.uuid4().hex[:8]}", "name": "Group readers", "permissions": [PERMISSION],
        "bindable_scopes": ["org"]})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _map(owner, group_id, role_id):
    bound = owner.post("/v1/identity/role-bindings", headers=csrf(owner), json={
        "subject_type": "group", "subject_id": group_id, "role_id": role_id})
    assert bound.status_code == 201, bound.text
    return bound.json()["id"]


def _refused(res):
    return res.status_code == 403 and res.json() == {"error": "forbidden", "permission": PERMISSION}


def test_a_scim_group_mapped_to_a_role_grants_it_until_scim_removes_the_member(real_pdp, engine):
    key = make_scim_key(engine)                       # also makes scim-owner-okta@example.test, an Owner
    scim = {"Authorization": f"Bearer {key}"}
    owner = _signed_in(engine, "scim-owner-okta@example.test")
    member_id = make_user(engine, "mia@example.test", role="viewer")
    with engine.begin() as db:                        # SCIM moves only the people it provisioned (M2)
        db.execute(text("UPDATE identity_principals SET created_by = 'sys_scim_okta' WHERE id = :p"),
                   {"p": member_id})
    member = _signed_in(engine, "mia@example.test")
    assert _refused(member.get(ROUTE))

    made = fresh_client().post("/scim/v2/Groups", headers=scim, json={
        "schemas": [SCIM_GROUP], "displayName": "Builders", "members": [{"value": member_id}]})
    assert made.status_code == 201, made.text
    group = made.json()["id"]
    assert _refused(member.get(ROUTE))                # a member, but the group grants nothing yet
    _map(owner, group, _group_reader_role(owner))
    assert member.get(ROUTE).status_code == 200

    removed = fresh_client().patch(f"/scim/v2/Groups/{group}", headers=scim, json={
        "schemas": [PATCH_OP], "Operations": [{"op": "remove", "path": f'members[value eq "{member_id}"]'}]})
    assert removed.status_code == 200, removed.text
    assert _refused(member.get(ROUTE))                # the very next request

    added = fresh_client().patch(f"/scim/v2/Groups/{group}", headers=scim, json={
        "schemas": [PATCH_OP], "Operations": [{"op": "add", "path": "members", "value": [{"value": member_id}]}]})
    assert added.status_code == 200, added.text
    assert member.get(ROUTE).status_code == 200
    assert fresh_client().delete(f"/scim/v2/Groups/{group}", headers=scim).status_code == 204
    assert _refused(member.get(ROUTE))
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE subject_id = :g"),
                          {"g": group}).scalar() == 0


def test_an_oidc_synced_group_grants_its_role_until_the_idp_stops_listing_it(real_pdp, engine, idp):
    from boss_engine.identity.authn_support import default_org_id

    make_user(engine, "boss-owner@example.test", role="owner")
    owner = _signed_in(engine, "boss-owner@example.test")
    provider = make_provider(engine, groups_claim="groups")
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source) VALUES "
                        "('grp_fxd_eng', :o, 'fxd-eng', 'Engineering', 'oidc')"), {"o": default_org_id(db)})
        db.execute(text("INSERT INTO identity_provider_group_mappings (provider_id, external_group, group_id) "
                        "VALUES (:p, 'eng', 'grp_fxd_eng')"), {"p": provider})
    _map(owner, "grp_fxd_eng", _group_reader_role(owner))
    invite(engine, "pat@corp.test", role="viewer")

    idp.claims = {"groups": ["eng", "unmapped"]}
    laptop = fresh_client()
    assert_signed_in(login_via(laptop, provider, idp)[0])
    assert laptop.get(ROUTE).status_code == 200

    idp.claims = {"groups": ["unmapped"]}
    phone = fresh_client()
    assert_signed_in(login_via(phone, provider, idp)[0])      # this sign-in syncs the removal
    assert _refused(phone.get(ROUTE))
    assert _refused(laptop.get(ROUTE))                        # and the older session's next request
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM identity_group_members WHERE group_id = 'grp_fxd_eng'")
                          ).scalar() == 0

    idp.claims = {"groups": ["eng"]}
    assert_signed_in(login_via(fresh_client(), provider, idp)[0])
    assert laptop.get(ROUTE).status_code == 200               # re-listed: granted again
