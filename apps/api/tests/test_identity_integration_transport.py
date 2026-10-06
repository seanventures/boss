"""Integration (lead): the legacy transport middleware and the identity guard agree on one request.

S1-8: under BOSS_REQUIRE_API_AUTH=true (every compose install) the legacy middleware refused anything
without the shared key BEFORE the guard ran, so sign-in was unreachable and a person's session never
reached a route. Now the identity routes that prove themselves are exempt, a NEW credential is
authenticated once by identity (the guard reuses it), and nothing else gets through without a key.
"""
from __future__ import annotations

import pytest

from apps.api.tests.identity_s1_world import (  # noqa: F401  (fixtures)
    HEADERS,
    csrf,
    fresh_client,
    make_user,
    sign_in,
    world,
)


from boss_engine.identity import authz as _authz

#: S1's world fakes the decision; these tests need S2's real one (captured before any fixture runs).
REAL = {name: getattr(_authz, name) for name in ("check", "effective_permissions", "bump_epoch")}


@pytest.fixture
def api_auth_required(monkeypatch):
    from apps.api.app import main
    from boss_engine.config import get_settings

    for name, fn in REAL.items():
        monkeypatch.setattr(_authz, name, fn)
    # main.py holds its own Settings instance (the legacy transport reads it); identity reads the
    # engine's. Both are set.
    for settings in (main.settings, get_settings()):
        monkeypatch.setattr(settings, "require_api_auth", True)
        monkeypatch.setattr(settings, "api_keys", "legacy-console-key-for-tests-0000000000")
        monkeypatch.setattr(settings, "api_key_principals",
                            "legacy-console-key-for-tests-0000000000:ceo")
        monkeypatch.setattr(settings, "legacy_console_api_key",
                            "legacy-console-key-for-tests-0000000000")
    yield get_settings()


def test_s1_8_sign_in_is_reachable_and_the_session_reaches_routes(world, engine, api_auth_required,
                                                                    monkeypatch):
    monkeypatch.setattr(api_auth_required, "authz_mode", "enforce")
    make_user(engine, "ann@example.test", role="owner", name="Ann")
    client = fresh_client()
    assert client.get("/v1/auth/options").status_code == 200       # public, before any key
    res = sign_in(client, "ann@example.test")
    assert res.status_code == 200, res.text
    me = client.get("/v1/identity/me")
    assert me.status_code == 200 and me.json()["email"] == "ann@example.test"
    assert isinstance(me.json()["epoch"], int)
    assert client.get("/v1/projects").status_code == 200           # a main.py route, by session
    # a write with the session but without the CSRF token is 403 (S1-1), not 401
    refused = client.post("/v1/identity/me/sessions/revoke-others", headers=HEADERS)
    assert refused.status_code in (403, 404, 405)


def test_without_any_credential_or_with_a_forged_cookie_nothing_gets_through(world, engine,
                                                                              api_auth_required):
    client = fresh_client()
    assert client.get("/v1/projects").status_code == 401
    client.cookies.set("__Host-boss_session", "bss_forged")
    assert client.get("/v1/projects").status_code == 401
    assert client.get("/v1/identity/me").status_code == 401


def test_in_shadow_a_session_without_a_legacy_seat_is_still_refused_by_the_legacy_checks(
        world, engine, api_auth_required, monkeypatch):
    """Shadow means the legacy checks still decide: a new person with no legacy member id is not a
    project member, so the session does not widen what a shadow-mode install allows."""
    monkeypatch.setattr(api_auth_required, "authz_mode", "shadow")
    make_user(engine, "bob@example.test", role="viewer", name="Bob")
    client = fresh_client()
    assert sign_in(client, "bob@example.test").status_code == 200
    assert client.get("/v1/identity/me").status_code == 200       # identity routes: always enforced
    listed = client.get("/v1/dashboard", params={"project_id": "boss-dogfood"})
    assert listed.status_code in (403, 404)


def test_removing_the_last_owner_on_any_path_is_a_409_never_a_500(world, engine):
    """The SCIM group that carries the only Owner binding is deleted: the deferred last-Owner
    trigger refuses the commit, and the API answers a conflict (SCIM-shaped on SCIM routes)."""
    from sqlalchemy import text

    from apps.api.tests.identity_s1_world import make_scim_key, mark_claimed, org_id
    from boss_engine.identity import users

    key = make_scim_key(engine)
    owner = make_user(engine, "only-owner@example.test", role=None)
    client = fresh_client()
    res = client.post("/scim/v2/Groups", headers={"Authorization": f"Bearer {key}"},
                      json={"displayName": "Owners", "externalId": "okta-owners",
                            "members": [{"value": owner}]})
    assert res.status_code == 201, res.text
    gid = res.json()["id"]
    with engine.begin() as db:
        users.add_binding(db, org_id=org_id(db), subject_id=gid, subject_type="group",
                          role_key="owner", granted_by="test")
    with engine.begin() as db:   # the group is the ONLY Owner reach (make_scim_key made another)
        db.execute(text("DELETE FROM identity_role_bindings WHERE role_id = 'arl_owner' AND "
                        "NOT (subject_type = 'group' AND subject_id = :g)"), {"g": gid})
    mark_claimed(engine)
    refused = client.delete(f"/scim/v2/Groups/{gid}", headers={"Authorization": f"Bearer {key}"})
    assert refused.status_code == 409, refused.text
    assert "Owner" in refused.text
    with engine.begin() as db:   # nothing was removed
        assert db.execute(text("SELECT 1 FROM identity_groups WHERE id = :g"), {"g": gid}).first()


def test_in_shadow_an_owner_acts_as_the_console_did_so_an_upgrade_changes_nothing(
        world, engine, api_auth_required, monkeypatch):
    """The console no longer carries the shared key; in shadow the legacy checks still decide, so
    the Owner (and the shared-password / network bridge people) act as the member the console's
    key acted as (ceo) — the console behaves as before until the routers are enforced."""
    monkeypatch.setattr(api_auth_required, "authz_mode", "shadow")
    make_user(engine, "olive@example.test", role="owner", name="Olive")
    client = fresh_client()
    assert sign_in(client, "olive@example.test").status_code == 200
    assert client.get("/v1/dashboard", params={"project_id": "boss-dogfood"}).status_code == 200
