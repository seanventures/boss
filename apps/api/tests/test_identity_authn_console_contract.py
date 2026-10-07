"""Stream S1: the shapes the console (S4) reads — AuthOptions, SignInStep, InvitationPreview,
Factor, LinkedIdentity, ApiKey/IssuedSecret, the TOTP QR — and the seams it asked for.
"""
from __future__ import annotations

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,

    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.identity import authn


def _fresh(engine, email, role):
    make_user(engine, email, role=role)
    client = fresh_client()
    sign_in(client, email)
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def test_auth_options_before_and_after_the_claim(world, engine):
    from apps.api.app import main

    main.announce_claim_link_once()          # what the API keeps live from boot (a GET never mints)
    client = fresh_client()
    opts = client.get("/v1/auth/options").json()
    assert set(opts) >= {"org_name", "host", "providers", "smtp", "signup", "legacy_password", "claim"}
    assert opts["claim"]["path"] == "setup_token" and opts["claim"]["expires_at"]
    assert "ceo" in opts["claim"]["seats"] and opts["smtp"] is False and opts["providers"] == []
    make_user(engine, "owner@example.test", role="owner")
    from apps.api.tests.identity_s1_world import mark_claimed

    mark_claimed(engine)
    assert client.get("/v1/auth/options").json()["claim"] is None


def test_sign_in_steps_carry_next_methods_and_who(world, engine):
    make_user(engine, "own@example.test", role="owner", name="Olu Owner")
    res = sign_in(fresh_client(), "own@example.test")
    body = res.json()
    assert body["next"] == "enrol" and body["methods"] == ["passkey", "totp"]
    assert body["who"]["display_name"] == "Olu Owner" and 0 <= body["who"]["hue"] < 360
    wrong = sign_in(fresh_client(), "own@example.test", "not-the-password-x")
    assert "who" not in wrong.json()                                    # nobody is greeted before proof


def test_invitation_preview_is_by_body_and_404_when_unknown(world, engine):
    owner = _fresh(engine, "inv@example.test", "owner")
    made = owner.post("/v1/identity/users", json={"email": "ana@example.test",
                                                  "bindings": [{"role_key": "operator", "scope_type": "org"}]},
                      headers=csrf(owner)).json()
    token = made["link"].split("#t=", 1)[1]
    client = fresh_client()
    preview = client.post("/v1/auth/invitations/preview", json={"token": token}, headers=HEADERS)
    assert preview.status_code == 200
    assert preview.json()["email"] == "ana@example.test" and preview.json()["roles"] == ["Operator"]
    assert token not in preview.text
    assert client.post("/v1/auth/invitations/preview", json={"token": "x" * 43},
                       headers=HEADERS).status_code == 404


def test_factors_qr_and_ending_every_other_session(world, engine):
    me = _fresh(engine, "fac@example.test", "operator")
    other = fresh_client()
    sign_in(other, "fac@example.test")
    enrol = me.post("/v1/identity/me/mfa/totp", headers=csrf(me))
    assert enrol.status_code == 200 and enrol.json()["qr_svg"].startswith("<svg")
    from apps.api.tests.identity_s1_world import totp_now

    confirm = me.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(enrol.json()["secret"])},
                      headers=csrf(me))
    assert confirm.status_code == 200
    factors = me.get("/v1/identity/me/factors").json()["factors"]
    assert {f["kind"] for f in factors} == {"totp", "recovery"}
    assert enrol.json()["secret"] not in str(factors)
    ended = me.delete("/v1/identity/me/sessions", headers=csrf(me))
    assert ended.status_code == 200 and ended.json()["revoked"] >= 1
    assert other.get("/v1/identity/me").status_code == 401 and me.get("/v1/identity/me").status_code == 200
    assert me.get("/v1/identity/me/identities").json() == {"identities": []}


def test_admin_seams_factors_sessions_filter_sa_scopes_and_rotation_by_replaces(world, engine):
    owner = _fresh(engine, "boss@example.test", "owner")
    pid = make_user(engine, "op@example.test", role="operator")
    sign_in(fresh_client(), "op@example.test")
    assert owner.get(f"/v1/identity/users/{pid}/factors").json() == {"factors": []}
    listed = owner.get(f"/v1/identity/sessions?principal_id={pid}").json()["sessions"]
    assert listed and {s["principal_id"] for s in listed} == {pid}
    owner_id = owner.get("/v1/identity/me").json()["principal_id"]
    sa = owner.post("/v1/identity/service-accounts", json={"name": "ci", "owner_principal_ids": [owner_id]},
                    headers=csrf(owner)).json()
    assert owner.get(f"/v1/identity/service-accounts/{sa['id']}/scopes").json() == {"permissions": []}
    renamed = owner.patch(f"/v1/identity/service-accounts/{sa['id']}", json={"name": "ci-bot"}, headers=csrf(owner))
    assert renamed.status_code == 200 and renamed.json()["display_name"] == "ci-bot"
    first = owner.post("/v1/identity/me/api-keys", json={"name": "k", "scopes": ["user:read"], "expires_days": 30},
                       headers=csrf(owner)).json()
    assert first["key"]["owner"]["id"] == owner_id and first["secret"].startswith("boss_pat_")
    second = owner.post("/v1/identity/me/api-keys", json={"name": "k", "scopes": ["user:read"],
                                                          "replaces": first["key"]["id"], "overlap_hours": 24},
                        headers=csrf(owner)).json()
    assert second["replaces"]["id"] == first["key"]["id"] and second["replaces"]["stops_at"]
