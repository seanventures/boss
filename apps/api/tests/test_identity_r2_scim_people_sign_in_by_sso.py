"""R2 #11: SSO and SCIM together.

  * A person SCIM-provisioned by a provider could not sign in through that same provider's SSO:
    the account existed unlinked, and an IdP asserting an existing account's email links nothing
    (A61/A89). The IdP vouched for that person twice — it provisioned them — so the first verified
    sign-in through THAT provider links the account and activates it. Never another provider's
    people, never an unverified email, never a disabled account.
  * An OIDC-synced group membership could be removed by hand (the next sign-in put it back):
    refused, 409 idp_membership_read_only.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests.identity_s1_world import fresh_client, world  # noqa: F401  (world is a fixture)
from apps.api.tests.test_identity_authn_sso import (  # noqa: F401  (idp is a fixture)
    assert_refused,
    assert_signed_in,
    idp,
    login_via,
    make_provider,
)
from boss_engine.identity import users
from boss_engine.identity.authn_support import default_org_id
from boss_engine.identity.scim import provisioning_principal


def _scim_person(engine, email: str, by_provider: str, *, status: str = "pending") -> str:
    with engine.begin() as db:
        return users.create_user(db, org_id=default_org_id(db), email=email, display_name=email.split("@")[0],
                                 status=status, created_by=provisioning_principal(by_provider))


def _linked(engine, pid: str) -> list[str]:
    with engine.begin() as db:
        return [r[0] for r in db.execute(text("SELECT provider_id FROM identity_user_identities "
                                              "WHERE principal_id = :p"), {"p": pid})]


def test_a_person_scim_provisioned_by_this_provider_signs_in_by_its_sso(world, engine, idp):
    provider = make_provider(engine)
    sam = _scim_person(engine, "sam@corp.test", provider)
    idp.claims = {"email": "sam@corp.test", "email_verified": True, "sub": "sam-sub"}
    client = fresh_client()
    assert_signed_in(login_via(client, provider, idp)[0])
    assert _linked(engine, sam) == [provider]
    with engine.begin() as db:
        assert users.by_id(db, sam).status == "active"
    assert client.get("/v1/identity/me").json()["email"] == "sam@corp.test"
    # the link is by (issuer, subject) from now on: a second sign-in reuses it
    assert_signed_in(login_via(fresh_client(), provider, idp)[0])
    assert _linked(engine, sam) == [provider]


def test_another_providers_people_and_unverified_emails_are_never_linked(world, engine, idp):
    provider, other = make_provider(engine), make_provider(engine, issuer="https://other-idp.test")
    kim = _scim_person(engine, "kim@corp.test", other)               # provisioned by ANOTHER provider
    idp.claims = {"email": "kim@corp.test", "email_verified": True, "sub": "kim-sub"}
    assert_refused(login_via(fresh_client(), provider, idp)[0], "not_invited")
    lee = _scim_person(engine, "lee@corp.test", provider)
    idp.claims = {"email": "lee@corp.test", "email_verified": False, "sub": "lee-sub"}
    assert_refused(login_via(fresh_client(), provider, idp)[0], "not_invited")
    assert _linked(engine, kim) == [] and _linked(engine, lee) == []


def test_a_disabled_scim_person_is_refused_with_words(world, engine, idp):
    provider = make_provider(engine)
    dee = _scim_person(engine, "dee@corp.test", provider, status="disabled")
    idp.claims = {"email": "dee@corp.test", "email_verified": True, "sub": "dee-sub"}
    res = login_via(fresh_client(), provider, idp)[0]
    assert_refused(res)
    assert res.status_code != 500 and _linked(engine, dee) == []


def test_an_idp_synced_membership_cannot_be_removed_by_hand(world, engine):
    from apps.api.app.main import app
    from apps.api.tests.identity_fixtures import IdentityWorld

    with engine.begin() as db:
        org = default_org_id(db)
        project = db.execute(text("SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                  "WHERE w.org_id = :o LIMIT 1"), {"o": org}).scalar()
    w = IdentityWorld(engine, org, "ws-unused", project, project)
    owner = w.person("r2-groups-owner")
    w.bind(owner, "owner")
    bob, carla = w.person("r2-bob"), w.person("r2-carla")
    gid = w.group("r2-mapped", members=(bob,), source="manual")
    with engine.begin() as db:
        db.execute(text("UPDATE identity_group_members SET source = 'oidc' WHERE group_id = :g AND principal_id = :p"),
                   {"g": gid, "p": bob.id})
        db.execute(text("INSERT INTO identity_group_members (group_id, principal_id, source) VALUES (:g, :p, 'manual')"),
                   {"g": gid, "p": carla.id})
    client = TestClient(app, raise_server_exceptions=False)
    with w.acting_as(w.ctx(owner)):
        synced = client.delete(f"/v1/identity/groups/{gid}/members/{bob.id}", headers={"sec-fetch-site": "same-origin"})
        manual = client.delete(f"/v1/identity/groups/{gid}/members/{carla.id}", headers={"sec-fetch-site": "same-origin"})
    assert synced.status_code == 409 and synced.json()["error"] == "idp_membership_read_only", synced.text
    assert manual.status_code == 200, manual.text


def test_scim_deactivating_a_signed_in_person_ends_their_session(world, engine, idp):
    from boss_engine.identity import scim
    from boss_engine.identity.principal import AuthContext, AuthMethod
    from boss_engine.identity.users import load_principal

    provider = make_provider(engine)
    eve = _scim_person(engine, "eve@corp.test", provider)
    idp.claims = {"email": "eve@corp.test", "email_verified": True, "sub": "eve-sub"}
    client = fresh_client()
    assert_signed_in(login_via(client, provider, idp)[0])
    assert client.get("/v1/identity/me").status_code == 200
    with engine.begin() as db:
        ctx = AuthContext(load_principal(db, provisioning_principal(provider)) or
                          _scim_principal(db, provider), AuthMethod.API_KEY)
        scim.patch_user(db, ctx, eve, {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                                       "Operations": [{"op": "replace", "path": "active", "value": False}]})
    assert client.get("/v1/identity/me").status_code == 401


def _scim_principal(db, provider):
    from boss_engine.identity.principal import Principal, PrincipalKind, PrincipalStatus

    org = default_org_id(db)
    pid = provisioning_principal(provider)
    db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                    "VALUES (:p, :o, 'system', 'active', 'SCIM') ON CONFLICT DO NOTHING"), {"p": pid, "o": org})
    return Principal(pid, PrincipalKind.SYSTEM, org, PrincipalStatus.ACTIVE, "SCIM")
