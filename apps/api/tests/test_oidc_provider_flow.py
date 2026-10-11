"""BOSS as an OIDC provider (F6, stream S6): the workforce sign-in, end to end through the real
app — discovery, JWKS, authorize, consent, token, userinfo, refresh, revocation, end_session."""
from __future__ import annotations

import pytest

from apps.api.tests.oidc_world import APP, LOGOUT, ORIGIN, REDIRECT, make_world, query_of


@pytest.fixture
def w(engine, monkeypatch):
    return make_world(engine, monkeypatch)


def test_discovery_says_code_flow_s256_and_the_issuer_from_the_public_url(w):
    doc = w.http.get(f"/oidc/{w.org}/.well-known/openid-configuration").json()
    assert doc["issuer"] == w.issuer == f"{ORIGIN}/oidc/{w.org}"
    assert doc["response_types_supported"] == ["code"]
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["authorization_response_iss_parameter_supported"] is True
    assert doc["id_token_signing_alg_values_supported"] == ["RS256", "EdDSA"]
    assert doc["request_uri_parameter_supported"] is False
    assert all(doc[k].startswith(w.issuer + "/") for k in ("authorization_endpoint", "token_endpoint",
                                                           "userinfo_endpoint", "jwks_uri", "end_session_endpoint"))


def test_a_forged_host_header_never_changes_the_issuer(w):
    doc = w.http.get(f"/oidc/{w.org}/.well-known/openid-configuration", headers={"Host": "evil.test",
                                                                                   "X-Forwarded-Host": "evil.test"}).json()
    assert doc["issuer"] == w.issuer


def test_an_unknown_org_has_no_issuer(w):
    assert w.http.get("/oidc/org_nope/.well-known/openid-configuration").status_code == 404


def test_jwks_publishes_the_oidc_family_public_keys_only(w):
    keys = w.jwks()["keys"]
    assert {k["alg"] for k in keys} == {"RS256", "EdDSA"}
    assert all(k["kid"].startswith("oidc-") for k in keys)
    assert not any(set(k) & {"d", "p", "q", "dp", "dq", "qi", "k"} for k in keys)
    boss = w.http.get("/.well-known/boss-jwks.json")
    if boss.status_code == 200:
        assert not {k["kid"] for k in keys} & {k.get("kid") for k in boss.json().get("keys", [])}


def test_the_whole_workforce_flow(w):
    alice = w.person("alice")
    client, secret = w.client()
    code, verifier, params = w.sign_in(client, alice)
    r = w.exchange(client, secret, code, verifier)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 600 and tok["refresh_token"].startswith("ort_")
    idt = w.verify(tok["id_token"], audience=client["id"])
    assert idt["nonce"] == params["nonce"] and idt["azp"] == client["id"]
    assert idt["acr"] == "urn:boss:acr:mfa" and "mfa" in idt["amr"]
    assert idt["sub"] != alice.principal.id and not idt["sub"].startswith("usr_")
    assert {"auth_time", "iat", "exp", "sid"} <= set(idt)
    at = w.verify(tok["access_token"], audience=client["id"], typ="at+jwt")
    assert at["client_id"] == client["id"] and at["sub"] == idt["sub"]
    info = w.userinfo(tok["access_token"])
    assert info.status_code == 200 and info.json()["sub"] == idt["sub"]
    assert info.json()["email"].startswith("alice-") and info.json()["name"] == "alice"
    # refresh: rotated, new tokens, same subject
    r2 = w.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=client, secret=secret)
    assert r2.status_code == 200, r2.text
    assert r2.json()["refresh_token"] != tok["refresh_token"]
    assert w.verify(r2.json()["id_token"], audience=client["id"])["sub"] == idt["sub"]
    # end_session with the hint: the grant ends, the app is sent back to its registered address
    out = w.http.get(f"/oidc/{w.org}/end_session", params={"id_token_hint": tok["id_token"],
                                                           "post_logout_redirect_uri": LOGOUT, "state": "bye"},
                     follow_redirects=False)
    assert out.status_code == 302 and out.headers["location"] == f"{LOGOUT}?state=bye"
    assert w.userinfo(r2.json()["access_token"]).status_code == 401
    r3 = w.token({"grant_type": "refresh_token", "refresh_token": r2.json()["refresh_token"]}, client=client, secret=secret)
    assert r3.json()["error"] == "invalid_grant"


def test_without_a_session_authorize_sends_the_person_to_sign_in_then_resumes(w):
    client, _ = w.client()
    r, _params = w.authorize(client["id"])
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"{ORIGIN}/login?next=%2Foidc%2F{w.org}%2Fresume%3Finteraction%3Doin_")
    nxt = query_of(loc)["next"]
    bob = w.person("bob")
    page = w.http.get(nxt, headers={"Cookie": f"__Host-boss_session={bob.cookie}"}, follow_redirects=False)
    assert page.status_code == 200 and "Bank wants to sign you in" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert APP in page.headers["content-security-policy"]       # form-action allows the app's redirect
    again = w.http.get(nxt, headers={"Cookie": f"__Host-boss_session={bob.cookie}"}, follow_redirects=False)
    assert again.status_code == 400                               # a resume link is single use
    done = w.consent(page, bob)
    assert done.status_code == 303 and done.headers["location"].startswith(REDIRECT + "?code=oac_")


def test_consent_is_remembered_and_prompt_none_then_succeeds(w):
    carol = w.person("carol")
    client, _ = w.client()
    w.sign_in(client, carol)
    r, _ = w.authorize(client["id"], who=carol, extra={"prompt": "none"})
    assert r.status_code == 302 and "code=" in r.headers["location"]


def test_an_eddsa_client_gets_eddsa_id_tokens_and_rs256_access_tokens(w):
    dan = w.person("dan")
    client, secret = w.client(id_token_alg="EdDSA")
    code, verifier, _ = w.sign_in(client, dan)
    tok = w.exchange(client, secret, code, verifier).json()
    import jwt

    assert jwt.get_unverified_header(tok["id_token"])["alg"] == "EdDSA"
    assert jwt.get_unverified_header(tok["access_token"])["alg"] == "RS256"
    assert w.verify(tok["id_token"], audience=client["id"])["sub"]


def test_a_public_client_signs_in_with_pkce_alone_and_rotates_refresh_tokens(w):
    erin = w.person("erin")
    client, secret = w.client(type="public", token_endpoint_auth_method="none")
    assert secret is None
    code, verifier, _ = w.sign_in(client, erin)
    r = w.exchange(client, None, code, verifier)
    assert r.status_code == 200, r.text
    r2 = w.token({"grant_type": "refresh_token", "refresh_token": r.json()["refresh_token"], "client_id": client["id"]})
    assert r2.status_code == 200, r2.text


def test_revocation_of_a_refresh_token_ends_the_grant(w):
    fay = w.person("fay")
    client, secret = w.client()
    code, verifier, _ = w.sign_in(client, fay)
    tok = w.exchange(client, secret, code, verifier).json()
    r = w.http.post(f"/oidc/{w.org}/revocation", content=f"token={tok['refresh_token']}",
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Authorization": _basic(client, secret)})
    assert r.status_code == 200
    assert w.userinfo(tok["access_token"]).status_code == 401
    unknown = w.http.post(f"/oidc/{w.org}/revocation", content="token=whatever",
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "Authorization": _basic(client, secret)})
    assert unknown.status_code == 200


def _basic(client, secret):
    import base64

    return "Basic " + base64.b64encode(f"{client['id']}:{secret}".encode()).decode()
