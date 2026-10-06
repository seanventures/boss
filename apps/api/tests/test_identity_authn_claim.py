"""Stream S1: claiming the first Owner on all three paths, invitations and people administration.

Adversarial ids: A14 (claim after an Owner exists is 404 for good), the network-bridge claim (origin
bound, 404 after the first Owner), A36 (mass assignment on PATCH user), last-Owner protection, the
Owner-only rules, J3 (API half: invite → accept → sign in).
"""
from __future__ import annotations

import re
import threading

from sqlalchemy import text

from apps.api.app import identity_guard
from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    csrf,
    fresh_client,
    make_network_operator,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import authn, claim

URL_RE = re.compile(r"/claim#t=(bst_[A-Za-z0-9_\-]+)")


def _setup_token(capsys) -> str:
    client = fresh_client()
    status = client.get("/v1/auth/setup/status")
    assert status.status_code == 200 and status.json()["claimed"] is False
    printed = capsys.readouterr().out
    m = URL_RE.search(printed)
    assert m, "the claim URL is printed to the logs, with the token in the fragment"
    assert "?" not in printed.split("/claim", 1)[1].split()[0]
    return m.group(1)


def _claim(client, **body):
    payload = {"name": "First Owner", "email": "first@example.test", "password": STRONG, **body}
    return client.post("/v1/auth/setup/claim", json=payload, headers=HEADERS)


def test_setup_token_claim_makes_the_first_owner_and_then_answers_404_for_good(world, engine, capsys):
    token = _setup_token(capsys)
    client = fresh_client()
    wrong = _claim(client, setup_token="bst_" + "x" * 43)
    assert wrong.status_code == 401
    res = _claim(client, setup_token=token)
    assert res.status_code == 200, res.text
    assert res.json()["mfa_enrolment_required"] is True
    me = client.get("/v1/identity/me").json()
    assert me["access_roles"] == ["owner"] and me["mfa_required"] is True
    assert {b["kind"] for b in me["banners"]} == {"mfa_enrolment_required"}
    assert world.events_of("bootstrap.claimed")
    # A14: every path answers 404 now, whatever it carries
    again = fresh_client()
    for body in ({"setup_token": token}, {"legacy_password": "anything"}, {}):
        r = _claim(again, email="second@example.test", **body)
        assert r.status_code == 404, (body, r.text)
    assert again.get("/v1/auth/setup/status").json() == {"claimed": True}
    # and no new setup link is minted or printed once claimed
    assert "claim#t=" not in capsys.readouterr().out


def test_the_setup_token_is_single_use_and_expires(world, engine, capsys):
    token = _setup_token(capsys)
    with engine.begin() as db:
        db.execute(text("UPDATE identity_auth_challenges SET expires_at = now() - interval '1 second' "
                        "WHERE kind = 'setup'"))
    assert _claim(fresh_client(), setup_token=token).status_code == 401
    with engine.begin() as db:
        assert claim.issue_setup_token(db, force=True)


def test_legacy_password_claim(world, engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "legacy_console_password", "the-old-shared-password")
    client = fresh_client()
    assert "legacy_password" in client.get("/v1/auth/setup/status").json()["paths"]
    assert _claim(client, legacy_password="not-it").status_code == 401
    ok = _claim(client, legacy_password="the-old-shared-password")
    assert ok.status_code == 200
    assert _claim(fresh_client(), legacy_password="the-old-shared-password",
                  email="x@example.test").status_code == 404


def test_network_bridge_claim_binds_the_browsers_origin(world, engine, monkeypatch):
    world.network_bridge = True
    make_network_operator(engine)
    monkeypatch.setattr(identity_guard, "_legacy_method", lambda: "network")
    monkeypatch.setattr(get_settings(), "public_url", "")            # shared-server: no BOSS_PUBLIC_URL
    client = fresh_client()
    status = client.get("/v1/auth/setup/status", headers={"Origin": ORIGIN})
    assert "network" in status.json()["paths"] and status.json()["origin"] == ORIGIN
    # the origin must be what the browser addressed, https, and confirmed on screen
    mismatched = client.post("/v1/auth/setup/claim", headers={"Origin": "https://evil.example"},
                             json={"name": "Op", "email": "op@example.test", "confirm_origin": "https://evil.example"})
    assert mismatched.status_code == 400 and mismatched.json()["error"] == "origin_mismatch"
    unconfirmed = client.post("/v1/auth/setup/claim", headers={"Origin": ORIGIN},
                              json={"name": "Op", "email": "op@example.test", "confirm_origin": "https://other.example"})
    assert unconfirmed.status_code == 400
    ok = client.post("/v1/auth/setup/claim", headers={"Origin": ORIGIN},
                     json={"name": "Op", "email": "op@example.test", "confirm_origin": ORIGIN})
    assert ok.status_code == 200, ok.text
    with engine.begin() as db:
        bound = db.execute(text("SELECT policy->>'public_origin' FROM identity_settings")).scalar()
    assert bound == ORIGIN
    assert world.events_of("bootstrap.claimed")[0].details == {"path": "network"}
    # once an Owner exists the network claim is gone for good
    later = fresh_client().post("/v1/auth/setup/claim", headers={"Origin": ORIGIN},
                                json={"name": "B", "email": "b@example.test", "confirm_origin": ORIGIN})
    assert later.status_code == 404


def test_a_request_that_is_not_network_bridged_cannot_use_the_network_path(world, engine):
    world.network_bridge = False
    res = fresh_client().post("/v1/auth/setup/claim", headers={"Origin": ORIGIN},
                              json={"name": "Op", "email": "op@example.test", "confirm_origin": ORIGIN})
    assert res.status_code == 401 and res.json()["error"] == "credential_required"


def test_claim_attempts_are_limited_to_ten_an_hour_per_ip(world, engine):
    client = fresh_client()
    statuses = [_claim(client, setup_token=f"bst_{i:043d}").status_code for i in range(11)]
    assert statuses[:10] == [401] * 10 and statuses[10] == 429


def test_two_simultaneous_claims_make_exactly_one_owner(world, engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "legacy_console_password", "race-password-value")
    results = []

    def go(i):
        results.append(_claim(fresh_client(), legacy_password="race-password-value",
                              email=f"racer{i}@example.test").status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 404]
    with engine.begin() as db:
        owners = db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE role_id = 'arl_owner'")).scalar()
    assert owners == 1


# ---- invitations (J3 API half) ---------------------------------------------------------------------

def _admin(engine, email: str, role: str):
    make_user(engine, email, role=role)
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def test_invite_accept_and_sign_in(world, engine):
    owner = _admin(engine, "inviter@example.test", "owner")
    res = owner.post("/v1/identity/users", json={"email": "new@example.test",
                                                 "bindings": [{"role_key": "operator", "scope_type": "org"}]},
                     headers=csrf(owner))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["emailed"] is False and body["shown_once"] is True
    token = body["link"].split("#t=", 1)[1]
    assert "invitation" in body["invitation_id"] or body["invitation_id"].startswith("inv_")
    listed = owner.get("/v1/identity/invitations").json()["invitations"]
    assert listed and token not in str(listed)
    newcomer = fresh_client()
    weak = newcomer.post("/v1/auth/invitations/accept", json={"token": token, "name": "New", "password": "short"},
                         headers=HEADERS)
    assert weak.status_code == 422
    ok = newcomer.post("/v1/auth/invitations/accept", json={"token": token, "name": "New", "password": STRONG},
                       headers=HEADERS)
    assert ok.status_code == 200, ok.text
    assert newcomer.get("/v1/identity/me").json()["access_roles"] == ["operator"]
    replay = fresh_client().post("/v1/auth/invitations/accept",
                                 json={"token": token, "name": "X", "password": STRONG}, headers=HEADERS)
    assert replay.status_code == 400
    assert sign_in(fresh_client(), "new@example.test").status_code == 200


def test_invited_roles_must_be_within_the_inviters(world, engine):
    admin = _admin(engine, "adm@example.test", "admin")
    res = admin.post("/v1/identity/users", json={"email": "o2@example.test",
                                                 "bindings": [{"role_key": "owner", "scope_type": "org"}]},
                     headers=csrf(admin))
    assert res.status_code == 403 and res.json()["error"] == "roles_exceed_inviter"
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_invitations")).scalar() == 0


def test_an_invitation_never_binds_to_an_existing_account(world, engine):
    owner = _admin(engine, "own@example.test", "owner")
    make_user(engine, "exists@example.test")
    res = owner.post("/v1/identity/users", json={"email": "exists@example.test", "bindings": []}, headers=csrf(owner))
    assert res.status_code == 409


# ---- administering people ----------------------------------------------------------------------------

def test_a36_patch_user_refuses_mass_assignment_and_changes_nothing(world, engine):
    owner = _admin(engine, "own2@example.test", "owner")
    target = make_user(engine, "t@example.test", role="viewer", name="Target")
    for field, value in (("role", "owner"), ("org_id", "org_x"), ("status", "disabled"), ("kind", "system"),
                         ("mfa_required", False)):
        res = owner.patch(f"/v1/identity/users/{target}", json={"display_name": "Changed", field: value},
                          headers=csrf(owner))
        assert res.status_code == 422 and res.json()["error"] == "field_not_allowed", field
    with engine.begin() as db:
        name, status = db.execute(text("SELECT display_name, status FROM identity_principals WHERE id = :p"),
                                  {"p": target}).first()
    assert (name, status) == ("Target", "active")
    ok = owner.patch(f"/v1/identity/users/{target}", json={"display_name": "Renamed"}, headers=csrf(owner))
    assert ok.status_code == 200 and ok.json()["display_name"] == "Renamed"


def test_the_last_owner_cannot_be_disabled_and_an_admin_cannot_touch_an_owner(world, engine):
    owner = _admin(engine, "solo@example.test", "owner")
    admin = _admin(engine, "adm3@example.test", "admin")
    owner_id = owner.get("/v1/identity/me").json()["principal_id"]
    for path in (f"/v1/identity/users/{owner_id}/disable", f"/v1/identity/users/{owner_id}/reset-mfa"):
        res = admin.post(path, headers=csrf(admin))
        assert res.status_code == 403 and res.json()["permission"] == "owner:assign", path
    last = owner.post(f"/v1/identity/users/{owner_id}/disable", headers=csrf(owner))
    assert last.status_code == 409 and last.json()["message"] == "Add another Owner first."
    assert owner.get("/v1/identity/me").status_code == 200


def test_reset_mfa_needs_another_owner_and_forces_re_enrolment(world, engine):
    first = _admin(engine, "o-a@example.test", "owner")
    second = _admin(engine, "o-b@example.test", "owner")
    second_id = second.get("/v1/identity/me").json()["principal_id"]
    own = second.post(f"/v1/identity/users/{second_id}/reset-mfa", headers=csrf(second))
    assert own.status_code == 403
    res = first.post(f"/v1/identity/users/{second_id}/reset-mfa", headers=csrf(first))
    assert res.status_code == 200 and res.json()["sessions_revoked"] >= 1
    assert second.get("/v1/identity/me").status_code == 401
    assert world.events_of("user.mfa_reset")


def test_another_orgs_person_is_not_found(world, engine):
    owner = _admin(engine, "own4@example.test", "owner")
    with engine.begin() as db:
        db.execute(text("INSERT INTO orgs (id, name) VALUES ('org_other_s1', 'Other') ON CONFLICT DO NOTHING"))
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "('usr_other_org_s1', 'org_other_s1', 'user', 'active', 'Stranger')"))
        db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES ('usr_other_org_s1', 'stranger@other.test')"))
    try:
        for method, path in (("get", "/v1/identity/users/usr_other_org_s1"),
                             ("post", "/v1/identity/users/usr_other_org_s1/disable")):
            kwargs = {"headers": csrf(owner)} if method == "post" else {}
            res = getattr(owner, method)(path, **kwargs)
            assert res.status_code == 404, path
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM identity_users WHERE principal_id = 'usr_other_org_s1'"))
            db.execute(text("DELETE FROM identity_principals WHERE id = 'usr_other_org_s1'"))
            db.execute(text("DELETE FROM orgs WHERE id = 'org_other_s1'"))


def test_a_passkey_first_claim_needs_no_password(world, engine, capsys):
    """Integration: the console's passkey path sends no password; the claim succeeds, the Owner
    has no password to phish, and must enrol MFA (the passkey) next."""
    token = _setup_token(capsys)
    client = fresh_client()
    res = client.post("/v1/auth/setup/claim", headers=HEADERS,
                      json={"name": "Pat Key", "email": "pat@example.test", "setup_token": token})
    assert res.status_code == 200, res.text
    assert res.json()["mfa_enrolment_required"] is True
    with engine.begin() as db:
        assert db.execute(text("SELECT password_hash FROM identity_users WHERE lower(email) = "
                               "'pat@example.test'")).scalar() is None
