"""OKTA AS A FIRST-CLASS SSO PROVIDER (BOSSAPP-1048), end to end on a fake Okta (fake_okta.py).

One Okta login across BOSS, Jira and other apps: the Okta preset and its issuers, the loopback dev
origin, groups → BOSS groups (from the id_token or, failing that, userinfo), a rotated signing key,
opt-in account linking, the Okta dashboard tile (OIDC Initiate login URI; SAML IdP-initiated),
safe landing pages, RP-initiated and back-channel logout, step-up re-authentication, and SCIM in
the shapes Okta sends. Every relaxation is opt-in per provider, off by default, and tested OFF too.
"""
from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.tests import fake_okta as ok
from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    fresh_client,
    make_scim_key,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from apps.api.tests.test_identity_authn_sso import IDP_ENTITY, SamlIdP, assert_refused, invite, post_acs
from boss_engine.identity import authn, http, sso_oidc

SUFFIX = "oktaacmeprovider0000000000"
PROVIDER_ID = f"idp_{SUFFIX}"


# ---- the world --------------------------------------------------------------------------------------

@pytest.fixture
def okta(world, monkeypatch):
    fake = ok.FakeOkta()
    monkeypatch.setattr(sso_oidc, "_fetch", fake.fetch(http.fetch))
    sso_oidc.clear_caches()

    def no_sockets(*a, **k):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(http, "_open_connection", no_sockets)
    monkeypatch.setattr(http, "_resolve", lambda host, port: ["93.184.216.34"])
    yield fake
    sso_oidc.clear_caches()


def okta_provider(engine, fake: ok.FakeOkta, *, pid: str = PROVIDER_ID, issuer: Optional[str] = None,
                  domains: tuple = ("acme.dev",), groups_claim: Optional[str] = "groups", trust_idp_mfa: bool = False,
                  link_verified_domain_accounts: bool = False, scim_adopt_existing_by_domain: bool = False,
                  protocol: str = "oidc", jit_mode: str = "off", **extra: Any) -> str:
    from boss_engine.identity.authn_support import default_org_id

    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO identity_providers (id, org_id, protocol, preset, name, issuer, client_id, allowed_algs, "
            "domains, jit_mode, trust_idp_mfa, groups_claim, scopes, link_verified_domain_accounts, "
            "scim_adopt_existing_by_domain, allow_idp_initiated, saml_certificates, saml_sso_url) VALUES (:id, :o, :p, "
            "'okta', 'Okta', :i, :c, '{RS256}', :d, :j, :t, :g, '{openid,email,profile,groups}', :l, :a, :aii, :certs, :sso)"),
            {"id": pid, "o": default_org_id(db), "p": protocol, "i": issuer or fake.issuer,
             "c": None if protocol == "saml" else fake.client_id, "d": list(domains), "j": jit_mode,
             "t": trust_idp_mfa, "g": groups_claim, "l": link_verified_domain_accounts,
             "a": scim_adopt_existing_by_domain, "aii": bool(extra.get("allow_idp_initiated")),
             "certs": list(extra.get("certificates", ())), "sso": extra.get("sso_url")})
    return pid


def start(client, fake: ok.FakeOkta, pid: str = PROVIDER_ID, **params: str) -> dict[str, str]:
    res = client.get(f"/v1/auth/sso/{pid}/start", params=params, follow_redirects=False)
    assert res.status_code == 302, res.text
    assert res.headers["location"].startswith(fake.endpoints["authorization_endpoint"] + "?")
    q = {k: v[0] for k, v in parse_qs(urlsplit(res.headers["location"]).query).items()}
    fake.nonce = q.get("nonce")
    return q


def callback(client, state: str, pid: str = PROVIDER_ID, **extra: str):
    return client.get(f"/v1/auth/sso/{pid}/callback", params={"code": "okta-code", "state": state, **extra},
                      follow_redirects=False)


def okta_sign_in(client, fake: ok.FakeOkta, pid: str = PROVIDER_ID, **params: str):
    q = start(client, fake, pid, **params)
    return callback(client, q["state"], pid), q


def landed(res, path: str = "/") -> None:
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"{ORIGIN}{path}", res.headers["location"]
    assert any(c.startswith(authn.SESSION_COOKIE + "=bss_") for c in res.headers.get_list("set-cookie"))


def owner_client(engine):
    make_user(engine, "own@acme.dev", role="owner")
    client = fresh_client()
    assert sign_in(client, "own@acme.dev").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client, {**HEADERS, "X-Boss-CSRF": client.get("/v1/identity/me").json()["csrf_token"]}


def map_group(engine, external: str, key: str = "builders") -> str:
    from boss_engine.identity import ids
    from boss_engine.identity.authn_support import default_org_id

    gid = ids.new_id("grp")
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source) VALUES (:g, :o, :k, :k, 'manual')"),
                   {"g": gid, "o": default_org_id(db), "k": key})
        db.execute(text("INSERT INTO identity_provider_group_mappings (provider_id, external_group, group_id) "
                        "VALUES (:p, :e, :g)"), {"p": PROVIDER_ID, "e": external, "g": gid})
    return gid


def members(engine, gid: str) -> set[str]:
    with engine.begin() as db:
        return {r[0] for r in db.execute(text("SELECT principal_id FROM identity_group_members WHERE group_id = :g"),
                                         {"g": gid})}


def principal_of(engine, email: str) -> str:
    with engine.begin() as db:
        return db.execute(text("SELECT principal_id FROM identity_users WHERE lower(email) = lower(:e)"),
                          {"e": email}).scalar()


# ---- 2. the Okta preset ----------------------------------------------------------------------------

def test_the_okta_preset_fills_groups_normalises_the_issuer_and_refuses_the_admin_host(world, engine, okta):
    client, h = owner_client(engine)
    body = {"protocol": "oidc", "preset": "okta", "name": "Okta", "client_id": okta.client_id,
            "client_secret": "okta-client-secret-value", "domains": ["acme.dev"]}
    refused = client.post("/v1/identity/providers", json={**body, "issuer": "https://acme-admin.okta.com"}, headers=h)
    assert refused.status_code == 422 and "-admin" in refused.json()["detail"]
    for bad in ("http://acme.okta.com", "https://acme.okta.com/app/boss", "https://acme.okta.com/oauth2/default?x=1"):
        assert client.post("/v1/identity/providers", json={**body, "issuer": bad}, headers=h).status_code == 422, bad
    res = client.post("/v1/identity/providers", json={**body, "issuer": "https://ACME.okta.com/"}, headers=h)
    assert res.status_code == 201, res.text
    made = res.json()
    assert made["issuer"] == "https://acme.okta.com"                                  # normalised once
    assert made["scopes"] == ["openid", "email", "profile", "groups"] and made["groups_claim"] == "groups"
    assert made["link_verified_domain_accounts"] is False and made["scim_adopt_existing_by_domain"] is False
    assert made["allow_idp_initiated"] is False                                       # every opt-in: off
    # the same issuer, spelled with a slash, is the same issuer (M1: one provider per issuer)
    again = client.post("/v1/identity/providers", json={**body, "issuer": "https://acme.okta.com/"}, headers=h)
    assert again.status_code == 409 and again.json()["error"] == "issuer_in_use"
    # a custom authorization server: no `groups` scope (it would refuse it), the groups claim stays
    custom = client.post("/v1/identity/providers", json={**body, "name": "Okta default AS",
                                                         "issuer": "https://acme.okta.com/oauth2/default/"}, headers=h)
    assert custom.status_code == 201, custom.text
    assert custom.json()["issuer"] == "https://acme.okta.com/oauth2/default"
    assert custom.json()["scopes"] == ["openid", "email", "profile"] and custom.json()["groups_claim"] == "groups"
    # allow_idp_initiated is SAML's; an OIDC provider answers with words
    assert client.post("/v1/identity/providers", json={**body, "name": "x", "issuer": "https://other.okta.com",
                                                       "allow_idp_initiated": True}, headers=h).status_code == 422


def test_an_okta_user_signs_in_and_okta_groups_become_boss_groups(world, engine, okta):
    okta_provider(engine, okta)
    gid = map_group(engine, "BOSS Engineers")
    invite(engine, "pat@acme.dev")
    client = fresh_client()
    res, q = okta_sign_in(client, okta)
    landed(res)
    assert set(q["scope"].split()) == {"openid", "email", "profile", "groups"}
    assert okta.token_requests[-1]["redirect_uri"] == [f"{ORIGIN}/v1/auth/sso/{PROVIDER_ID}/callback"]
    pat = principal_of(engine, "pat@acme.dev")
    assert members(engine, gid) == {pat}
    with engine.begin() as db:
        issuer, subject = db.execute(text("SELECT issuer, subject FROM identity_user_identities")).one()
    assert (issuer, subject) == (okta.issuer, "00u1okta0pat")
    # Okta drops the person from the group: the next sign-in removes the membership
    okta.claims = {"groups": ["Everyone"]}
    landed(okta_sign_in(fresh_client(), okta)[0])
    assert members(engine, gid) == set()


def test_groups_only_in_userinfo_are_read_from_userinfo_for_the_same_subject(world, engine, okta):
    okta_provider(engine, okta)
    gid = map_group(engine, "BOSS Engineers")
    invite(engine, "pat@acme.dev")
    okta.claims = {"groups": None}                       # an access-token-only groups claim
    okta.userinfo = {"groups": ["BOSS Engineers"]}
    landed(okta_sign_in(fresh_client(), okta)[0])
    assert okta.userinfo_calls == 1 and members(engine, gid) == {principal_of(engine, "pat@acme.dev")}
    # userinfo answering for SOMEONE ELSE grants nothing
    okta.userinfo = {"sub": "00u-someone-else", "groups": ["BOSS Engineers"]}
    landed(okta_sign_in(fresh_client(), okta)[0])
    assert members(engine, gid) == set()


def test_issuers_are_compared_normalised_on_both_sides_and_never_loosely(world, engine, monkeypatch):
    slashed = ok.FakeOkta(issuer_trailing_slash=True)        # discovery says ".../" ; tokens say "..."
    monkeypatch.setattr(sso_oidc, "_fetch", slashed.fetch(http.fetch))
    monkeypatch.setattr(http, "_resolve", lambda host, port: ["93.184.216.34"])
    sso_oidc.clear_caches()
    okta_provider(engine, slashed)
    invite(engine, "pat@acme.dev")
    landed(okta_sign_in(fresh_client(), slashed)[0])
    # the RFC 9207 `iss` parameter with a trailing slash matches; another path never does
    client = fresh_client()
    q = start(client, slashed)
    landed(callback(client, q["state"], iss=slashed.issuer + "/"))
    client = fresh_client()
    q = start(client, slashed)
    assert_refused(callback(client, q["state"], iss=slashed.issuer + "/oauth2/default"), "issuer_mismatch")
    # an id_token from the custom authorization server is not one from the org server
    slashed.claims = {"iss": slashed.issuer + "/oauth2/default"}
    assert_refused(okta_sign_in(fresh_client(), slashed)[0], "issuer_mismatch")
    sso_oidc.clear_caches()


def test_a_rotated_okta_signing_key_is_fetched_and_an_unpublished_one_refused(world, engine, okta):
    okta_provider(engine, okta)
    invite(engine, "pat@acme.dev")
    landed(okta_sign_in(fresh_client(), okta)[0])
    okta.rotate(keep_old=True)
    # The cached JWKS predates the rotation; an unknown kid refetches (at most once a minute).
    with sso_oidc._LOCK:                                   # noqa: SLF001 — age the cache past the throttle
        uri = okta.endpoints["jwks_uri"]
        fetched, doc = sso_oidc._JWKS[uri]                 # noqa: SLF001
        sso_oidc._JWKS[uri] = (fetched - sso_oidc.JWKS_REFETCH_MIN_SECONDS - 1, doc)  # noqa: SLF001
    landed(okta_sign_in(fresh_client(), okta)[0])
    # a token signed by a key Okta never published, under a published kid, is refused
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    stranger = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = fresh_client()
    q = start(client, okta)
    forged = okta._sign({"iss": okta.issuer, "aud": okta.client_id, "sub": "00u1okta0pat", "iat": int(time.time()),
                         "exp": int(time.time()) + 60, "nonce": okta.nonce}, key=stranger)
    original = okta.id_token
    okta.id_token = lambda: forged                         # type: ignore[method-assign]
    try:
        assert_refused(callback(client, q["state"]), "id_token_invalid")
    finally:
        okta.id_token = original                           # type: ignore[method-assign]


# ---- 1. the loopback dev origin --------------------------------------------------------------------

def test_a_loopback_console_can_start_sso_before_a_public_origin_is_bound_and_a_remote_one_cannot(world, engine, okta, monkeypatch):
    from apps.api.app.main import app
    from boss_engine.config import get_settings

    monkeypatch.setattr(get_settings(), "public_url", "")
    okta_provider(engine, okta)
    for base in ("http://localhost:3000", "http://127.0.0.1:3010"):
        res = TestClient(app, base_url=base).get(f"/v1/auth/sso/{PROVIDER_ID}/start", follow_redirects=False)
        assert res.status_code == 302, (base, res.text)
        q = parse_qs(urlsplit(res.headers["location"]).query)
        assert q["redirect_uri"] == [f"{base}/v1/auth/sso/{PROVIDER_ID}/callback"]
    for base in ("http://boss.lan:3000", "http://192.168.1.20:3000", "https://boss.remote.example"):
        res = TestClient(app, base_url=base).get(f"/v1/auth/sso/{PROVIDER_ID}/start", follow_redirects=False)
        assert res.status_code == 409 and res.json()["error"] == "public_origin_unset", base
    # the loopback flow finishes, landing on the loopback console with a session
    invite(engine, "pat@acme.dev")
    local = TestClient(app, base_url="http://localhost:3000")
    res = local.get(f"/v1/auth/sso/{PROVIDER_ID}/start", follow_redirects=False)
    preauth = [c for c in res.headers.get_list("set-cookie") if c.startswith(authn.PREAUTH_COOKIE)][0].split(";")[0]
    q = {k: v[0] for k, v in parse_qs(urlsplit(res.headers["location"]).query).items()}
    okta.nonce = q["nonce"]
    done = local.get(f"/v1/auth/sso/{PROVIDER_ID}/callback", params={"code": "c", "state": q["state"]},
                     headers={"Cookie": preauth}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "http://localhost:3000/", done.headers["location"]
    assert okta.token_requests[-1]["redirect_uri"] == [f"http://localhost:3000/v1/auth/sso/{PROVIDER_ID}/callback"]


# ---- 3. account linking ----------------------------------------------------------------------------

def test_an_existing_google_users_okta_sign_in_is_refused_unless_linking_is_on(world, engine, okta):
    make_user(engine, "pat@acme.dev", role="operator")            # already in BOSS (Google or a password)
    okta_provider(engine, okta)
    assert_refused(okta_sign_in(fresh_client(), okta)[0], "not_invited")
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_user_identities")).scalar() == 0
    assert not world.events_of("sso.link.auto")


def test_linking_on_links_only_a_verified_email_in_the_providers_domains_and_audits_it(world, engine, okta):
    pat = make_user(engine, "pat@acme.dev", role="operator")
    make_user(engine, "sam@elsewhere.test", role="operator")
    okta_provider(engine, okta, link_verified_domain_accounts=True)
    okta.claims = {"email_verified": False}
    assert_refused(okta_sign_in(fresh_client(), okta)[0], "not_invited")       # unverified: never
    okta.claims = {"email": "sam@elsewhere.test", "sub": "00u-sam"}
    assert_refused(okta_sign_in(fresh_client(), okta)[0], "not_invited")       # not its domain: never
    okta.claims = {}
    client = fresh_client()
    landed(okta_sign_in(client, okta)[0])
    assert client.get("/v1/identity/me").json()["email"] == "pat@acme.dev"
    with engine.begin() as db:
        assert db.execute(text("SELECT principal_id FROM identity_user_identities WHERE provider_id = :p"),
                          {"p": PROVIDER_ID}).scalar() == pat
    linked = world.events_of("sso.link.auto")
    assert len(linked) == 1 and linked[0].target_id == pat
    assert linked[0].details["rule"] == "link_verified_domain_accounts"
    # a second Okta subject claiming the same email never takes the linked person over
    okta.claims = {"sub": "00u-impostor"}
    assert_refused(okta_sign_in(fresh_client(), okta)[0], "not_invited")
    assert len(world.events_of("sso.link.auto")) == 1


# ---- 4. the Okta dashboard tile and safe landing pages ----------------------------------------------

def test_the_okta_tile_starts_sign_in_and_lands_only_on_a_same_origin_page(world, engine, okta):
    okta_provider(engine, okta)
    invite(engine, "pat@acme.dev")
    client = fresh_client()
    res, _ = okta_sign_in(client, okta, iss=okta.issuer, target_link_uri=f"{ORIGIN}/projects?tab=runs")
    landed(res, "/projects?tab=runs")
    # the issuer must be THIS provider's (normalised): another Okta org's tile is refused
    bad = fresh_client().get(f"/v1/auth/sso/{PROVIDER_ID}/start", params={"iss": "https://evil.okta.com"},
                             follow_redirects=False)
    assert bad.status_code == 400 and bad.json()["error"] == "issuer_mismatch"
    landed(okta_sign_in(fresh_client(), okta, iss=okta.issuer + "/")[0])
    # a landing page on another origin, or any unsafe spelling, lands on the home page instead
    for target in ("https://evil.test/steal", "//evil.test/x", "/\\evil.test", "/v1/identity/me", "javascript:alert(1)"):
        res, _ = okta_sign_in(fresh_client(), okta, next=target)
        landed(res, "/")
    landed(okta_sign_in(fresh_client(), okta, next="/runs/run_1")[0], "/runs/run_1")


def test_saml_idp_initiated_sign_in_is_refused_unless_opted_in_then_signed_once_and_for_this_audience(world, engine):
    idp = SamlIdP()
    okta_provider(engine, ok.FakeOkta(), protocol="saml", issuer=IDP_ENTITY, domains=("corp.test",),
                  certificates=(idp.pem,), sso_url="https://saml-idp.example.com/sso")
    invite(engine, "pat@corp.test")

    def unsolicited(**kw: Any) -> str:
        xml = idp.assertion_xml(in_response_to="", **kw).replace(' InResponseTo=""', "")
        return idp.encode(idp.response(idp.sign(xml)))

    assert_refused(post_acs(fresh_client(), unsolicited()), "saml_unsolicited")          # off by default
    with engine.begin() as db:
        db.execute(text("UPDATE identity_providers SET allow_idp_initiated = true WHERE id = :p"), {"p": PROVIDER_ID})
    payload = unsolicited()
    client = fresh_client()
    res = client.post("/saml/v2/acs", data={"SAMLResponse": payload, "RelayState": "/projects"}, follow_redirects=False)
    landed(res, "/projects")
    assert_refused(post_acs(fresh_client(), payload), "saml_replay")                     # each assertion once
    assert_refused(post_acs(fresh_client(), unsolicited(audience="https://other-sp.example")), "audience_mismatch")
    res = fresh_client().post("/saml/v2/acs", data={"SAMLResponse": unsolicited(), "RelayState": "https://evil.test/"},
                              follow_redirects=False)
    landed(res, "/")


# ---- 5. logout and step-up ---------------------------------------------------------------------------

def test_signing_out_of_an_okta_session_signs_out_of_okta_with_the_id_token_hint(world, engine, okta):
    okta_provider(engine, okta)
    invite(engine, "pat@acme.dev")
    client = fresh_client()
    landed(okta_sign_in(client, okta)[0])
    hint = okta.issued_id_tokens[-1]
    with engine.begin() as db:
        sealed = db.execute(text("SELECT sso_provider_id, sso_sid, sso_id_token_sealed FROM identity_sessions "
                                 "WHERE revoked_at IS NULL")).one()
    assert sealed.sso_provider_id == PROVIDER_ID and sealed.sso_sid == okta.sid
    assert hint.encode() not in bytes(sealed.sso_id_token_sealed)                   # sealed, never plain
    out = client.post("/v1/auth/logout", headers=HEADERS)
    assert out.status_code == 200, out.text
    url = out.json()["end_session_url"]
    assert url.startswith(okta.endpoints["end_session_endpoint"] + "?")
    q = parse_qs(urlsplit(url).query)
    assert q["id_token_hint"] == [hint] and q["post_logout_redirect_uri"] == [f"{ORIGIN}/login"]
    assert client.get("/v1/identity/me").status_code == 401
    # a password session has no IdP to sign out of
    make_user(engine, "own@acme.dev", role="operator")
    pw = fresh_client()
    assert sign_in(pw, "own@acme.dev").status_code == 200
    assert "end_session_url" not in pw.post("/v1/auth/logout", headers=HEADERS).json()


def _backchannel(token: str, pid: str = PROVIDER_ID):
    return fresh_client().post(f"/v1/auth/sso/{pid}/backchannel-logout", data={"logout_token": token})


def test_okta_back_channel_logout_ends_exactly_the_sessions_it_names_once(world, engine, okta):
    okta_provider(engine, okta)
    invite(engine, "pat@acme.dev")
    first, second = fresh_client(), fresh_client()
    first_sid = okta.sid
    landed(okta_sign_in(first, okta)[0])
    okta.sid = "102-second-session"
    landed(okta_sign_in(second, okta)[0])
    # forged, wrong audience, wrong issuer, not a logout event, a nonce, stale, no subject: nothing ends
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    stranger = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in (okta.logout_token(sid=first_sid, key=stranger), okta.logout_token(sid=first_sid, aud="other-client"),
                okta.logout_token(sid=first_sid, iss="https://evil.okta.com"),
                okta.logout_token(sid=first_sid, events={"urn:other": {}}),
                okta.logout_token(sid=first_sid, nonce="n"), okta.logout_token(sid=first_sid, iat=int(time.time()) - 3600),
                okta.logout_token(sub=None), "not-a-jwt"):
        res = _backchannel(bad)
        assert res.status_code == 400 and res.headers["cache-control"] == "no-store"
    assert first.get("/v1/identity/me").status_code == 200 and second.get("/v1/identity/me").status_code == 200
    token = okta.logout_token(sid=first_sid)
    assert _backchannel(token).status_code == 200
    assert first.get("/v1/identity/me").status_code == 401                         # that session ended
    assert second.get("/v1/identity/me").status_code == 200                        # the other did not
    assert _backchannel(token).status_code == 400                                  # replayed jti
    assert _backchannel(okta.logout_token(sub="00u1okta0pat")).status_code == 200  # by subject: all of them
    assert second.get("/v1/identity/me").status_code == 401
    events = world.events_of("sso.logout.backchannel")
    assert [e.details.get("sessions_ended") for e in events if e.outcome.value == "success"] == [1, 1]


def test_step_up_through_okta_needs_a_fresh_trusted_mfa_sign_in_of_the_same_person(world, engine, okta):
    okta_provider(engine, okta, trust_idp_mfa=True)
    invite(engine, "pat@acme.dev")
    okta.claims = {"amr": ["pwd", "mfa"]}
    client = fresh_client()
    landed(okta_sign_in(client, okta)[0])
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET mfa_at = now() - interval '1 hour'"))
    q = start(client, okta, purpose="reauth", next="/identity/people")
    assert q["prompt"] == "login" and q["max_age"] == "0"
    landed(callback(client, q["state"]), "/identity/people")
    with engine.begin() as db:
        live = db.execute(text("SELECT mfa_at > now() - interval '1 minute' AS fresh, sso_provider_id "
                               "FROM identity_sessions WHERE revoked_at IS NULL")).one()
    assert live.fresh and live.sso_provider_id == PROVIDER_ID                       # rotated, still federated
    # an old Okta session (auth_time long ago) is not a re-authentication
    okta.claims = {"amr": ["pwd", "mfa"], "auth_time": int(time.time()) - 3600}
    q = start(client, okta, purpose="reauth")
    assert_refused(callback(client, q["state"]), "reauthentication_required")
    # without MFA at Okta there is no step-up
    okta.claims = {"amr": ["pwd"]}
    q = start(client, okta, purpose="reauth")
    assert_refused(callback(client, q["state"]), "idp_mfa_required")
    # and nobody steps up without a session
    res = fresh_client().get(f"/v1/auth/sso/{PROVIDER_ID}/start", params={"purpose": "reauth"}, follow_redirects=False)
    assert res.status_code == 401


# ---- 6. SCIM, in Okta's shapes -----------------------------------------------------------------------

def _scim(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/scim+json"}


def test_okta_scim_discovery_and_its_test_suite_operations(world, engine, okta):
    okta_provider(engine, okta)
    key = make_scim_key(engine, SUFFIX)
    c = fresh_client()
    spc = c.get("/scim/v2/ServiceProviderConfig", headers=_scim(key)).json()
    assert spc["patch"]["supported"] is True and spc["bulk"]["supported"] is False
    assert spc["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    assert {r["id"] for r in c.get("/scim/v2/ResourceTypes", headers=_scim(key)).json()["Resources"]} == {"User", "Group"}
    schema_ids = {s["id"] for s in c.get("/scim/v2/Schemas", headers=_scim(key)).json()["Resources"]}
    assert schema_ids == {ok.USER_SCHEMA, ok.GROUP_SCHEMA}
    assert c.get("/scim/v2/ServiceProviderConfig").status_code in (401, 403)           # still needs the key

    # Okta's first call: does the user exist?
    found = c.get("/scim/v2/Users", params={"filter": ok.scim_filter_username("nia@acme.dev"),
                                            "startIndex": 1, "count": 100}, headers=_scim(key)).json()
    assert found["totalResults"] == 0 and found["Resources"] == []
    # create, with a pushed password: accepted, ignored, never stored
    made = c.post("/scim/v2/Users", json=ok.scim_create_user("nia@acme.dev", given="Nia", family="Ode",
                                                              external_id="00u-nia"), headers=_scim(key))
    assert made.status_code == 201, made.text
    user = made.json()
    assert user["name"]["givenName"] == "Nia" and user["name"]["familyName"] == "Ode"
    assert user["externalId"] == "00u-nia" and user["emails"][0] == {"value": "nia@acme.dev", "primary": True, "type": "work"}
    assert "password" not in user
    with engine.begin() as db:
        assert db.execute(text("SELECT password_hash FROM identity_users WHERE principal_id = :p"),
                          {"p": user["id"]}).scalar() is None
    assert world.events_of("scim.password_ignored")
    assert "Pushed-Pa55word!" not in str([e.details for e in world.events])
    # PATCH replace with a path, add without one, a password patch: all 200
    uid = user["id"]
    r = c.patch(f"/scim/v2/Users/{uid}", json=ok.scim_patch_name("Niamh"), headers=_scim(key))
    assert r.status_code == 200 and r.json()["name"] == {"formatted": "Niamh Ode", "givenName": "Niamh", "familyName": "Ode"}
    r = c.patch(f"/scim/v2/Users/{uid}", json=ok.scim_patch_add_without_path("Ode-Smith", "00u-nia-2"), headers=_scim(key))
    assert r.status_code == 200 and r.json()["name"]["familyName"] == "Ode-Smith" and r.json()["externalId"] == "00u-nia-2"
    assert c.patch(f"/scim/v2/Users/{uid}", json=ok.scim_patch_password(), headers=_scim(key)).status_code == 200
    # PUT (Okta's profile push) and the externalId filter
    r = c.put(f"/scim/v2/Users/{uid}", json=ok.scim_put_user(uid, "nia@acme.dev", given="Nia", family="Ode",
                                                             external_id="00u-nia-3"), headers=_scim(key))
    assert r.status_code == 200 and r.json()["externalId"] == "00u-nia-3"
    by_ext = c.get("/scim/v2/Users", params={"filter": 'externalId eq "00u-nia-3"'}, headers=_scim(key)).json()
    assert [u["id"] for u in by_ext["Resources"]] == [uid]
    # pagination: startIndex / count
    for i in range(3):
        assert c.post("/scim/v2/Users", json=ok.scim_create_user(f"p{i}@acme.dev", external_id=f"00u-p{i}",
                                                                 password=None), headers=_scim(key)).status_code == 201
    page = c.get("/scim/v2/Users", params={"startIndex": 2, "count": 2}, headers=_scim(key)).json()
    assert page["startIndex"] == 2 and page["itemsPerPage"] == 2 and page["totalResults"] >= 4
    # deactivate (Okta unassigns the app): disabled
    r = c.patch(f"/scim/v2/Users/{uid}", json=ok.scim_deactivate(), headers=_scim(key))
    assert r.status_code == 200 and r.json()["active"] is False
    with engine.begin() as db:
        assert db.execute(text("SELECT status FROM identity_principals WHERE id = :p"), {"p": uid}).scalar() == "disabled"
    assert c.patch(f"/scim/v2/Users/{uid}", json=ok.scim_reactivate(), headers=_scim(key)).json()["active"] is True
    # roles are still never SCIM's to set
    assert c.patch(f"/scim/v2/Users/{uid}", headers=_scim(key), json={
        "schemas": [ok.PATCH_SCHEMA], "Operations": [{"op": "add", "path": "roles", "value": [{"value": "owner"}]}]}
    ).status_code == 400


def test_scim_adopts_existing_people_only_when_opted_in_and_never_an_owner(world, engine, okta):
    okta_provider(engine, okta)
    key = make_scim_key(engine, SUFFIX)
    lee = make_user(engine, "lee@acme.dev", role="operator")
    boss = make_user(engine, "boss@acme.dev", role="owner")
    outsider = make_user(engine, "kim@elsewhere.test", role="operator")
    c = fresh_client()
    # off (the default): an existing person is not SCIM's
    assert c.post("/scim/v2/Users", json=ok.scim_create_user("lee@acme.dev"), headers=_scim(key)).status_code == 409
    assert c.patch(f"/scim/v2/Users/{lee}", json=ok.scim_deactivate(), headers=_scim(key)).status_code == 403
    with engine.begin() as db:
        db.execute(text("UPDATE identity_providers SET scim_adopt_existing_by_domain = true WHERE id = :p"),
                   {"p": PROVIDER_ID})
    # on: Okta's create for someone BOSS has adopts them (201, the same person)
    made = c.post("/scim/v2/Users", json=ok.scim_create_user("lee@acme.dev", given="Lee", family="Ray",
                                                              external_id="00u-lee"), headers=_scim(key))
    assert made.status_code == 201 and made.json()["id"] == lee and made.json()["externalId"] == "00u-lee"
    adopted = world.events_of("scim.user.adopted")
    assert len(adopted) == 1 and adopted[0].target_id == lee
    # group push with the adopted person
    grp = c.post("/scim/v2/Groups", json=ok.scim_push_group("BOSS Engineers", [lee]), headers=_scim(key))
    assert grp.status_code == 201, grp.text
    assert [m["value"] for m in grp.json()["members"]] == [lee]
    gid = grp.json()["id"]
    assert c.patch(f"/scim/v2/Groups/{gid}", json=ok.scim_group_rename(gid, "Engineers"), headers=_scim(key)).json()["displayName"] == "Engineers"
    assert c.patch(f"/scim/v2/Groups/{gid}", json=ok.scim_group_remove_member(lee), headers=_scim(key)).json()["members"] == []
    assert c.patch(f"/scim/v2/Groups/{gid}", json=ok.scim_group_add_members([lee]), headers=_scim(key)).status_code == 200
    # and deprovisioning now reaches them
    r = c.patch(f"/scim/v2/Users/{lee}", json=ok.scim_deactivate(), headers=_scim(key))
    assert r.status_code == 200 and r.json()["active"] is False
    # never an Owner, never another domain, never into a group by the back door
    assert c.patch(f"/scim/v2/Users/{boss}", json=ok.scim_deactivate(), headers=_scim(key)).status_code == 403
    assert c.post("/scim/v2/Users", json=ok.scim_create_user("boss@acme.dev"), headers=_scim(key)).status_code == 403
    assert c.patch(f"/scim/v2/Users/{outsider}", json=ok.scim_deactivate(), headers=_scim(key)).status_code == 403
    assert c.patch(f"/scim/v2/Groups/{gid}", json=ok.scim_group_add_members([outsider]), headers=_scim(key)).status_code == 403
    with engine.begin() as db:
        status = dict(db.execute(text("SELECT id, status FROM identity_principals WHERE id = ANY(:i)"),
                                 {"i": [boss, outsider]}).fetchall())
    assert status == {boss: "active", outsider: "active"}


def test_an_okta_provisioned_person_signs_in_through_okta_and_okta_deprovisions_them(world, engine, okta):
    okta_provider(engine, okta)
    key = make_scim_key(engine, SUFFIX)
    made = fresh_client().post("/scim/v2/Users", json=ok.scim_create_user("pat@acme.dev"), headers=_scim(key))
    assert made.status_code == 201
    client = fresh_client()
    landed(okta_sign_in(client, okta)[0])
    assert client.get("/v1/identity/me").json()["email"] == "pat@acme.dev"
    assert fresh_client().patch(f"/scim/v2/Users/{made.json()['id']}", json=ok.scim_deactivate(),
                                headers=_scim(key)).status_code == 200
    assert client.get("/v1/identity/me").status_code == 401
    assert_refused(okta_sign_in(fresh_client(), okta)[0], "account_disabled")
