"""BOSS as an OIDC provider: the named adversarial list (F6 §6.8, stream S6), against the real app.

A1 code replay · A2 PKCE downgrade or missing verifier · A3 redirect_uri tricks · A4 mix-up ·
A5 refresh reuse -> family revoked · A6 algorithm and token-family confusion · A7 audience/azp and
client assertions · A8 nonce · A9 cross-client and cross-pool subject isolation · A10 an app
credential on a BOSS route · A11 CSRF on authorize and consent · A12 token leakage in logs and URLs ·
A13 client authentication downgrade and brute force · A14 a person who loses the grant ·
A15 open redirect at logout · A16 parameters BOSS never fetches or accepts twice.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import quote

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from sqlalchemy import text

from apps.api.tests.oidc_world import APP, LOGOUT, ORIGIN, REDIRECT, b64u, expire_codes, make_world, pkce, query_of
from boss_engine.db import begin


@pytest.fixture
def w(engine, monkeypatch):
    return make_world(engine, monkeypatch)


def _signed_in(w, name="alice", **client_over):
    person = w.person(name)
    client, secret = w.client(**client_over)
    code, verifier, params = w.sign_in(client, person)
    return person, client, secret, code, verifier, params


def _tokens(w, name="alice", **client_over):
    person, client, secret, code, verifier, params = _signed_in(w, name, **client_over)
    r = w.exchange(client, secret, code, verifier)
    assert r.status_code == 200, r.text
    return person, client, secret, r.json(), params


def _no_redirect(r):
    assert r.status_code == 400 and "location" not in r.headers, (r.status_code, r.headers.get("location"))


def _basic(client, secret):
    return "Basic " + base64.b64encode(f"{client['id']}:{secret}".encode()).decode()


def _forge(header: dict, payload: dict, key: bytes, alg: str = "HS256") -> str:
    h = b64u(json.dumps(header).encode())
    p = b64u(json.dumps(payload).encode())
    if alg == "none":
        return f"{h}.{p}."
    sig = hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{b64u(sig)}"


# ---- A1 code replay ------------------------------------------------------------------------------

def test_a1_a_replayed_code_is_refused_and_revokes_everything_issued_from_it(w):
    _, client, secret, code, verifier, _ = _signed_in(w)
    first = w.exchange(client, secret, code, verifier)
    assert first.status_code == 200
    again = w.exchange(client, secret, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert w.userinfo(first.json()["access_token"]).status_code == 401
    refresh = w.token({"grant_type": "refresh_token", "refresh_token": first.json()["refresh_token"]},
                      client=client, secret=secret)
    assert refresh.json()["error"] == "invalid_grant"
    with begin() as db:
        events = db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                                 "event_type = 'oidc.code.replayed'"), {"o": w.org}).scalar()
    assert events == 1


def test_a1_an_expired_code_is_refused(w):
    _, client, secret, code, verifier, _ = _signed_in(w)
    expire_codes()
    assert w.exchange(client, secret, code, verifier).json()["error"] == "invalid_grant"


# ---- A2 PKCE -------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"drop": ("code_challenge",)}, {"drop": ("code_challenge_method",)},
    {"extra": {"code_challenge_method": "plain"}}, {"extra": {"code_challenge": "short"}},
    {"extra": {"code_challenge_method": "s256"}},
])
def test_a2_authorize_refuses_any_pkce_but_s256(w, change):
    alice = w.person("alice")
    client, _ = w.client()
    r, params = w.authorize(client["id"], who=alice, **change)
    assert r.status_code == 302
    q = query_of(r.headers["location"])
    assert q["error"] == "invalid_request" and q["iss"] == w.issuer and q["state"] == params["state"]
    assert "code" not in q


@pytest.mark.parametrize("verifier", [None, "", "x" * 42, "wrong" * 10, "a" * 129, "spaces are not allowed " * 3])
def test_a2_the_token_endpoint_wants_the_matching_verifier(w, verifier):
    _, client, secret, code, _good, _ = _signed_in(w)
    r = w.exchange(client, secret, code, _good, code_verifier=verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a2_a_verifier_from_another_request_does_not_redeem_this_code(w):
    _, client, secret, code, _, _ = _signed_in(w)
    other_verifier, _ = pkce()
    assert w.exchange(client, secret, code, other_verifier).json()["error"] == "invalid_grant"


# ---- A3 redirect_uri tricks ----------------------------------------------------------------------

@pytest.mark.parametrize("uri", [
    REDIRECT + "x", REDIRECT + "/", REDIRECT + "/../evil", f"{APP}/callback/../../evil", f"{APP}/other",
    REDIRECT + "?next=https://evil.test", REDIRECT + "#frag", REDIRECT.replace("https", "http"),
    REDIRECT.replace("app.example.test", "APP.example.test"), REDIRECT.replace(".test/", ".test:443/"),
    f"https://app.example.test@evil.test/callback", f"https://evil.test/?{REDIRECT}", "https://evil.test/callback",
    REDIRECT.replace("callback", "%63allback"), "", "javascript:alert(1)",
])
def test_a3_an_unregistered_redirect_uri_gets_a_boss_page_never_a_redirect(w, uri):
    alice = w.person("alice")
    client, _ = w.client()
    r, _ = w.authorize(client["id"], who=alice, redirect_uri=uri)
    _no_redirect(r)
    assert "text/html" in r.headers["content-type"] and "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_a3_a_missing_redirect_uri_or_unknown_client_is_a_page(w):
    alice = w.person("alice")
    client, _ = w.client()
    r, _ = w.authorize(client["id"], who=alice, drop=("redirect_uri",))
    _no_redirect(r)
    r, _ = w.authorize("boc_nobody", who=alice)
    _no_redirect(r)


def test_a3_a_repeated_redirect_uri_or_client_id_is_a_page(w):
    alice = w.person("alice")
    client, _ = w.client()
    _, challenge = pkce()
    base = (f"response_type=code&client_id={client['id']}&scope=openid&nonce=n&code_challenge={challenge}"
            f"&code_challenge_method=S256&redirect_uri={quote(REDIRECT, safe='')}")
    r, _ = w.authorize(client["id"], who=alice, raw_query=base + "&redirect_uri=" + quote("https://evil.test/", safe=""))
    _no_redirect(r)
    r, _ = w.authorize(client["id"], who=alice, raw_query=base + "&client_id=boc_other")
    _no_redirect(r)


def test_a3_the_token_endpoint_wants_the_same_redirect_uri(w):
    _, client, secret, code, verifier, _ = _signed_in(w, redirect_uris=[REDIRECT, f"{APP}/second"])
    assert w.exchange(client, secret, code, verifier, redirect_uri=f"{APP}/second").json()["error"] == "invalid_grant"


@pytest.mark.parametrize("uri", [
    "https://*.example.test/cb", "https://app.example.test/cb#x", "http://app.example.test/cb",
    "https://u:p@app.example.test/cb", "https://app.example.test/a/../cb", "https://app.example.test/a/%2e%2e/cb",
    "/relative", "app.example.test/cb", "custom-scheme://cb", "https://app.example.test/a b", "https:///cb",
    "https://app.example.test/a%2fb",
])
def test_a3_registration_refuses_redirect_uris_that_could_become_open_redirects(w, uri):
    with pytest.raises(ValueError):
        w.client(redirect_uris=[uri])


def test_a3_loopback_http_is_the_only_plain_http(w):
    client, _ = w.client(redirect_uris=["http://localhost:4000/cb", "http://127.0.0.1:4000/cb", "http://[::1]:4000/cb"])
    assert len(client["redirect_uris"]) == 3


# ---- A4 mix-up -----------------------------------------------------------------------------------

def test_a4_every_authorization_response_names_the_issuer(w):
    alice = w.person("alice")
    client, _ = w.client()
    ok, _ = w.authorize(client["id"], who=alice, extra={"prompt": "consent"})
    assert ok.status_code == 200
    err, _ = w.authorize(client["id"], who=alice, extra={"response_type": "token"})
    assert query_of(err.headers["location"])["iss"] == w.issuer
    assert query_of(err.headers["location"])["error"] == "unsupported_response_type"


def test_a4_a_code_from_one_issuer_is_worthless_at_another(w, engine):
    from boss_engine.oidc_provider import pools as oidc_pools
    from apps.api.tests.identity_fixtures import IdentityWorld

    _, client, secret, code, verifier, _ = _signed_in(w)
    other = IdentityWorld.create(engine)
    with begin() as db:
        oidc_pools.create(db, org_id=other.org, actor="usr_test", body={"name": "Other", "kind": "workforce"})
    r = w.token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier},
                client=client, secret=secret, issuer_org=other.org)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert w.exchange(client, secret, code, verifier).status_code == 200      # untouched at its own issuer


# ---- A5 refresh reuse ----------------------------------------------------------------------------

def test_a5_a_reused_refresh_token_revokes_the_whole_family(w):
    _, client, secret, tok, _ = _tokens(w)
    first = tok["refresh_token"]
    second = w.token({"grant_type": "refresh_token", "refresh_token": first}, client=client, secret=secret).json()
    reuse = w.token({"grant_type": "refresh_token", "refresh_token": first}, client=client, secret=secret)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    after = w.token({"grant_type": "refresh_token", "refresh_token": second["refresh_token"]}, client=client, secret=secret)
    assert after.json()["error"] == "invalid_grant"
    assert w.userinfo(second["access_token"]).status_code == 401
    with begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                               "event_type = 'oidc.token.reuse_detected'"), {"o": w.org}).scalar() >= 1


def test_a5_no_refresh_token_without_offline_access_and_a_refresh_cannot_widen_scopes(w):
    alice = w.person("alice")
    client, secret = w.client()
    code, verifier, _ = w.sign_in(client, alice, scope="openid email")
    tok = w.exchange(client, secret, code, verifier).json()
    assert "refresh_token" not in tok
    _, client2, secret2, tok2, _ = _tokens(w, "bob")
    r = w.token({"grant_type": "refresh_token", "refresh_token": tok2["refresh_token"], "scope": "openid email"},
                client=client2, secret=secret2)
    assert r.status_code == 200 and r.json()["scope"] == "openid email"
    wider = w.token({"grant_type": "refresh_token", "refresh_token": r.json()["refresh_token"],
                     "scope": "openid email phone"}, client=client2, secret=secret2)
    assert wider.json()["error"] == "invalid_scope"


# ---- A6 algorithm and token-family confusion -----------------------------------------------------

def _rsa_public_pem(w) -> bytes:
    key = next(k for k in w.jwks()["keys"] if k["alg"] == "RS256")
    return jwt.PyJWK(key).key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def test_a6_alg_none_and_hs256_with_the_public_key_are_refused_at_userinfo(w):
    _, client, _, tok, _ = _tokens(w)
    header = jwt.get_unverified_header(tok["access_token"])
    claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
    for alg, key in (("none", b""), ("HS256", _rsa_public_pem(w)),
                     ("HS256", json.dumps(next(k for k in w.jwks()["keys"] if k["alg"] == "RS256")).encode())):
        forged = _forge({**header, "alg": alg}, {**claims, "scope": "openid profile email roles"}, key, alg)
        r = w.userinfo(forged)
        assert r.status_code == 401 and r.json()["error"] == "invalid_token", alg


def test_a6_a_token_signed_by_another_key_under_a_real_kid_is_refused(w):
    _, _, _, tok, _ = _tokens(w, id_token_alg="EdDSA")
    claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
    eddsa_kid = next(k["kid"] for k in w.jwks()["keys"] if k["alg"] == "EdDSA")
    rogue = ed25519.Ed25519PrivateKey.generate()
    forged = jwt.encode(claims, rogue, algorithm="EdDSA", headers={"kid": eddsa_kid, "typ": "at+jwt"})
    assert w.userinfo(forged).status_code == 401
    # an access token claiming EdDSA under the real EdDSA kid is refused even if it verified: ATs are RS256 only
    rs_kid = next(k["kid"] for k in w.jwks()["keys"] if k["alg"] == "RS256")
    ec_key = ec.generate_private_key(ec.SECP256R1())
    assert w.userinfo(jwt.encode(claims, ec_key, algorithm="ES256", headers={"kid": rs_kid, "typ": "at+jwt"})).status_code == 401


def test_a6_an_id_token_is_not_an_access_token_and_an_access_token_is_not_an_id_token_hint(w):
    _, client, _, tok, _ = _tokens(w)
    assert w.userinfo(tok["id_token"]).status_code == 401
    r = w.http.get(f"/oidc/{w.org}/end_session", params={"id_token_hint": tok["access_token"],
                                                         "post_logout_redirect_uri": LOGOUT}, follow_redirects=False)
    _no_redirect(r)
    assert w.userinfo(tok["access_token"]).status_code == 200      # the forged logout ended nothing


def test_a6_a_boss_internal_style_token_is_refused_at_userinfo(w):
    _tokens(w)
    rogue = ed25519.Ed25519PrivateKey.generate()
    now = int(time.time())
    for typ, kid in (("boss-run+jwt", "run-abc"), ("at+jwt", "run-abc"), ("boss-actor+jwt", "actor-abc")):
        forged = jwt.encode({"iss": w.issuer, "sub": "usr_x", "iat": now, "exp": now + 60, "scope": "openid",
                             "sid": "ogr_x", "client_id": "boc_x", "aud": "boss:api"}, rogue, algorithm="EdDSA",
                            headers={"kid": kid, "typ": typ})
        assert w.userinfo(forged).status_code == 401


def test_a6_an_app_token_or_code_is_never_a_boss_credential(w):
    person, _, _, tok, _ = _tokens(w)
    for credential in (tok["access_token"], tok["id_token"], tok["refresh_token"]):
        r = w.http.get("/v1/identity/me", headers={"Authorization": f"Bearer {credential}"})
        assert r.status_code == 401, credential[:8]
    # and the console's session is never an app access token
    assert w.userinfo(person.cookie).status_code == 401


def test_a6_the_keys_live_apart(w):
    _tokens(w)
    from boss_engine.identity import signing_keys

    kid = next(k["kid"] for k in w.jwks()["keys"] if k["alg"] == "EdDSA")
    with begin() as db:
        for family in ("run", "actor"):
            assert signing_keys.verification_key(kid, family, db=db) is None
        assert db.execute(text("SELECT count(*) FROM identity_signing_keys WHERE kid LIKE 'oidc-%'")).scalar() == 0


# ---- A7 audience, azp and client assertions --------------------------------------------------------

def test_a7_id_tokens_name_exactly_their_client(w):
    _, client, _, tok, _ = _tokens(w)
    claims = jwt.decode(tok["id_token"], options={"verify_signature": False})
    assert claims["aud"] == client["id"] and claims["azp"] == client["id"]
    _, client2, _, tok2, _ = _tokens(w, "bob", resource="https://api.bank.test")
    assert jwt.decode(tok2["access_token"], options={"verify_signature": False})["aud"] == "https://api.bank.test"


def test_a7_logout_with_one_clients_hint_and_another_client_id_is_refused(w):
    _, client, _, tok, _ = _tokens(w)
    other, _ = w.client(name="Other")
    r = w.http.get(f"/oidc/{w.org}/end_session", params={"id_token_hint": tok["id_token"], "client_id": other["id"]},
                   follow_redirects=False)
    _no_redirect(r)


def _jwt_client(w, alg="ES256"):
    priv = ec.generate_private_key(ec.SECP256R1()) if alg == "ES256" else ed25519.Ed25519PrivateKey.generate()
    jwk = json.loads(jwt.algorithms.get_default_algorithms()[alg].to_jwk(priv.public_key()))
    jwk["kid"] = "k1"
    client, secret = w.client(token_endpoint_auth_method="private_key_jwt", jwks={"keys": [jwk]})
    assert secret is None
    return client, priv


def _assertion(w, client, priv, alg="ES256", **over):
    now = int(time.time())
    claims = {"iss": client["id"], "sub": client["id"], "aud": f"{w.issuer}/token", "iat": now, "exp": now + 60,
              "jti": b64u(hashlib.sha256(str(time.time_ns()).encode()).digest())}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, priv, algorithm=alg, headers={"kid": "k1"})


def test_a7_private_key_jwt_signs_in_and_refuses_every_bad_assertion(w):
    alice = w.person("alice")
    client, priv = _jwt_client(w)
    T = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

    def exchange(assertion, code, verifier):
        return w.token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                        "code_verifier": verifier, "client_assertion_type": T, "client_assertion": assertion})

    bad = [
        _assertion(w, client, priv, aud="https://evil.test/token"),
        _assertion(w, client, priv, iss="boc_someone_else"),
        _assertion(w, client, priv, exp=int(time.time()) + 3600),
        _assertion(w, client, priv, exp=int(time.time()) - 600),
        _assertion(w, client, priv, jti=None),
        _assertion(w, client, ec.generate_private_key(ec.SECP256R1())),
        _forge({"alg": "HS256", "kid": "k1"}, {"iss": client["id"], "sub": client["id"], "aud": f"{w.issuer}/token",
                                               "exp": int(time.time()) + 60, "jti": "j"}, b"k1"),
        _forge({"alg": "none", "kid": "k1"}, {"iss": client["id"], "sub": client["id"], "aud": f"{w.issuer}/token",
                                              "exp": int(time.time()) + 60, "jti": "j2"}, b"", "none"),
    ]
    for assertion in bad:
        code, verifier, _ = w.sign_in(client, alice)
        r = exchange(assertion, code, verifier)
        assert r.status_code == 401 and r.json()["error"] == "invalid_client", r.text
    code, verifier, _ = w.sign_in(client, alice)
    good = _assertion(w, client, priv)
    assert exchange(good, code, verifier).status_code == 200
    code, verifier, _ = w.sign_in(client, alice)
    replay = exchange(good, code, verifier)
    assert replay.status_code == 401         # a jti is single use
    w.ids  # noqa: B018


def test_a7_registration_refuses_private_or_symmetric_keys_and_any_url(w):
    priv = ec.generate_private_key(ec.SECP256R1())
    full = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(priv))
    for jwks in ({"keys": [full]}, {"keys": [{"kty": "oct", "k": "c2VjcmV0"}]}, {"keys": []},
                 {"keys": [{**json.loads(jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key())), "x5u": "https://evil.test"}]}):
        with pytest.raises(ValueError):
            w.client(token_endpoint_auth_method="private_key_jwt", jwks=jwks)


# ---- A8 nonce -------------------------------------------------------------------------------------

def test_a8_nonce_is_required_and_bound_to_its_code(w):
    alice = w.person("alice")
    client, secret = w.client()
    r, _ = w.authorize(client["id"], who=alice, drop=("nonce",))
    assert query_of(r.headers["location"])["error"] == "invalid_request"
    code1, v1, p1 = w.sign_in(client, alice)
    code2, v2, p2 = w.sign_in(client, alice)
    t2 = w.exchange(client, secret, code2, v2).json()
    t1 = w.exchange(client, secret, code1, v1).json()
    assert jwt.decode(t1["id_token"], options={"verify_signature": False})["nonce"] == p1["nonce"]
    assert jwt.decode(t2["id_token"], options={"verify_signature": False})["nonce"] == p2["nonce"]
    refreshed = w.token({"grant_type": "refresh_token", "refresh_token": t1["refresh_token"]}, client=client, secret=secret)
    assert "nonce" not in jwt.decode(refreshed.json()["id_token"], options={"verify_signature": False})


# ---- A9 subject isolation ------------------------------------------------------------------------

def test_a9_pairwise_subjects_differ_per_client_and_public_ones_per_pool(w):
    alice = w.person("alice")
    subs = {}
    for name, stype in (("one", "pairwise"), ("two", "pairwise"), ("pub1", "public"), ("pub2", "public")):
        client, secret = w.client(name=name, subject_type=stype)
        code, verifier, _ = w.sign_in(client, alice)
        subs[name] = jwt.decode(w.exchange(client, secret, code, verifier).json()["id_token"],
                                options={"verify_signature": False})["sub"]
    assert subs["one"] != subs["two"] and subs["pub1"] == subs["pub2"] != subs["one"]
    assert alice.principal.id not in subs.values()
    from boss_engine.oidc_provider import pools as oidc_pools

    with begin() as db:
        other_pool = oidc_pools.create(db, org_id=w.org, actor="usr_test", body={"name": "Other", "kind": "workforce"})
    client3, secret3 = w.client(name="pub3", subject_type="public", pool_id=other_pool["id"])
    code, verifier, _ = w.sign_in(client3, alice)
    sub3 = jwt.decode(w.exchange(client3, secret3, code, verifier).json()["id_token"],
                      options={"verify_signature": False})["sub"]
    assert sub3 != subs["pub1"]


def test_a9_a_code_or_refresh_token_is_bound_to_its_client(w):
    _, client_a, secret_a, code, verifier, _ = _signed_in(w)
    client_b, secret_b = w.client(name="B")
    stolen = w.exchange(client_b, secret_b, code, verifier)
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
    assert w.exchange(client_a, secret_a, code, verifier).json()["error"] == "invalid_grant"   # the grant is burnt
    _, client_c, secret_c, tok, _ = _tokens(w, "bob")
    r = w.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=client_b, secret=secret_b)
    assert r.json()["error"] == "invalid_grant"
    assert w.userinfo(tok["access_token"]).status_code == 401
    # revoking another client's token does nothing (and says nothing)
    _, client_d, secret_d, tok_d, _ = _tokens(w, "carol")
    w.http.post(f"/oidc/{w.org}/revocation", content=f"token={tok_d['refresh_token']}",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": _basic(client_b, secret_b)})
    assert w.userinfo(tok_d["access_token"]).status_code == 200


# ---- A10 an app credential on a BOSS route -------------------------------------------------------

def test_a10_an_app_session_cookie_is_not_a_console_session(w):
    r = w.http.get("/v1/identity/me", headers={"Cookie": "__Host-boss_app_session=bas_" + "x" * 40})
    assert r.status_code == 401


# ---- A11 CSRF ------------------------------------------------------------------------------------

def test_a11_the_consent_post_needs_the_origin_the_session_and_its_token(w):
    alice = w.person("alice")
    mallory = w.person("mallory")
    client, _ = w.client()
    page, _ = w.authorize(client["id"], who=alice)
    assert page.status_code == 200
    for kw in ({"origin": None}, {"origin": "https://evil.test"}, {"fetch_site": "cross-site"},
               {"csrf": "forged"}):
        r = w.consent(page, alice, **kw)
        assert r.status_code == 403 and "location" not in r.headers, kw
    assert w.consent(page, mallory).status_code == 403       # another person cannot spend alice's consent
    m_page, _ = w.authorize(client["id"], who=mallory)
    import re

    m_csrf = re.search(r'name="csrf" value="([^"]+)"', m_page.text).group(1)
    assert w.consent(page, alice, csrf=m_csrf).status_code == 403
    ok = w.consent(page, alice)
    assert ok.status_code == 303
    assert w.consent(page, alice).status_code == 400          # an interaction is single use


def test_a11_prompt_none_never_consents_for_the_person(w):
    alice = w.person("alice")
    client, _ = w.client()
    r, _ = w.authorize(client["id"], who=alice, extra={"prompt": "none"})
    assert query_of(r.headers["location"])["error"] == "consent_required"
    r, _ = w.authorize(client["id"], extra={"prompt": "none"})
    assert query_of(r.headers["location"])["error"] == "login_required"


def test_a11_declining_consent_tells_the_app_nothing_but_access_denied(w):
    alice = w.person("alice")
    client, _ = w.client()
    page, _ = w.authorize(client["id"], who=alice)
    r = w.consent(page, alice, decision="deny")
    q = query_of(r.headers["location"])
    assert q["error"] == "access_denied" and "code" not in q


def test_a11_first_party_skips_consent_but_never_for_offline_access(w):
    alice = w.person("alice")
    client, _ = w.client(first_party=True)
    r, _ = w.authorize(client["id"], who=alice, extra={"scope": "openid email"})
    assert r.status_code == 302 and "code=" in r.headers["location"]
    r, _ = w.authorize(client["id"], who=alice)
    assert r.status_code == 200 and "stay signed in" in r.text


def test_a11_hosted_pages_cannot_be_framed(w):
    alice = w.person("alice")
    client, _ = w.client()
    page, _ = w.authorize(client["id"], who=alice)
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "script-src" not in page.headers["content-security-policy"]       # default-src 'none' covers it


# ---- A12 leakage ---------------------------------------------------------------------------------

def test_a12_no_code_token_or_secret_reaches_a_log_or_the_audit_trail(w, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    _, client, secret, tok, _ = _tokens(w)
    r = w.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=client, secret=secret)
    w.userinfo(r.json()["access_token"])
    w.exchange(client, "wrong-secret", "oac_nothing", "v" * 43)
    out = capsys.readouterr()
    logs = caplog.text + out.out + out.err
    with begin() as db:
        audit_rows = " ".join(json.dumps(r[0]) for r in db.execute(text(
            "SELECT details FROM identity_security_events WHERE org_id = :o"), {"o": w.org}))
    for value in (secret, tok["access_token"], tok["id_token"], tok["refresh_token"], r.json()["refresh_token"]):
        assert value not in logs and value not in audit_rows
    for prefix in ("oac_", "ort_", "eyJ"):
        assert prefix not in audit_rows


def test_a12_codes_and_tokens_are_hashed_at_rest(w):
    _, client, secret, tok, _ = _tokens(w)
    with begin() as db:
        dump = json.dumps([list(map(str, r)) for t in ("oidc_auth_codes", "oidc_refresh_tokens", "oidc_interactions")
                           for r in db.execute(text(f"SELECT * FROM {t}"))])
        secret_rows = json.dumps([list(map(str, r)) for r in db.execute(text("SELECT * FROM oidc_clients"))])
    assert tok["refresh_token"] not in dump and "oac_" not in dump
    assert secret not in secret_rows


def test_a12_an_access_token_is_never_accepted_from_a_url(w):
    _, _, _, tok, _ = _tokens(w)
    r = w.http.get(f"/oidc/{w.org}/userinfo", params={"access_token": tok["access_token"]})
    assert r.status_code == 400 and tok["access_token"] not in r.text


def test_a12_the_authorization_response_carries_only_code_state_and_iss(w):
    alice = w.person("alice")
    client, _ = w.client(first_party=True)
    r, _ = w.authorize(client["id"], who=alice, extra={"scope": "openid email"})
    assert set(query_of(r.headers["location"])) == {"code", "state", "iss"}
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"


# ---- A13 client authentication -------------------------------------------------------------------

def test_a13_a_client_authenticates_only_by_its_registered_method(w):
    _, client, secret, code, verifier, _ = _signed_in(w)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
    assert w.token({**form, "client_id": client["id"]}).status_code == 401                         # none
    assert w.token({**form, "client_id": client["id"], "client_secret": secret}).status_code == 401  # post
    assert w.token(form, client=client, secret=secret + "x").status_code == 401
    both = w.token({**form, "client_secret": secret}, client=client, secret=secret)
    assert both.status_code == 400
    r = w.token(form, client=client, secret="nope")
    assert r.headers["www-authenticate"].startswith("Basic")
    assert "www-authenticate" not in w.token({**form, "client_id": client["id"]}).headers   # no Bearer challenge
    public, _ = w.client(name="spa", type="public", token_endpoint_auth_method="none")
    assert w.token({**form, "client_id": public["id"]}, client=public, secret="anything").status_code == 401


def test_a13_ten_bad_secrets_lock_the_client_and_say_so(w):
    _, client, secret, code, verifier, _ = _signed_in(w)
    for _ in range(10):
        assert w.exchange(client, "bad", code, verifier).status_code == 401
    assert w.exchange(client, secret, code, verifier).status_code == 401        # even the right one, for now
    with begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                               "event_type = 'oidc.client.locked'"), {"o": w.org}).scalar() >= 1


def test_a13_a_rotated_secret_stops_working(w):
    from boss_engine.oidc_provider import clients as oidc_clients

    _, client, secret, code, verifier, _ = _signed_in(w)
    with begin() as db:
        _, new_secret = oidc_clients.rotate_secret(db, org_id=w.org, actor="usr_test", client_id=client["id"])
    assert w.exchange(client, secret, code, verifier).status_code == 401
    assert w.exchange(client, new_secret, code, verifier).status_code == 200


# ---- A14 a person who loses the grant ------------------------------------------------------------

def test_a14_without_oidc_app_sign_in_there_is_no_sign_in(w):
    nobody = w.person("nobody", role=None)
    client, _ = w.client()
    r, _ = w.authorize(client["id"], who=nobody)
    assert query_of(r.headers["location"])["error"] == "access_denied"
    other_project = w.person("elsewhere", project="b")
    r, _ = w.authorize(client["id"], who=other_project)
    assert query_of(r.headers["location"])["error"] == "access_denied"


def test_a14_unbinding_or_disabling_the_person_ends_refresh_and_userinfo(w):
    person, client, secret, tok, _ = _tokens(w)
    with begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE id = :p"), {"p": person.principal.id})
    assert w.userinfo(tok["access_token"]).status_code == 401
    r = w.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=client, secret=secret)
    assert r.json()["error"] == "invalid_grant"


def test_a14_a_disabled_client_issues_nothing(w):
    from boss_engine.oidc_provider import clients as oidc_clients

    _, client, secret, tok, _ = _tokens(w)
    with begin() as db:
        oidc_clients.update(db, org_id=w.org, actor="usr_test", client_id=client["id"], body={"status": "disabled"})
    assert w.userinfo(tok["access_token"]).status_code == 401
    assert w.token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, client=client,
                   secret=secret).status_code == 401


def test_a14_a_legacy_shared_password_session_is_nobody_and_cannot_sign_in(w):
    from boss_engine.identity import sessions
    from boss_engine.identity.legacy import LEGACY_SHARED_PRINCIPAL

    with begin() as db:
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "(:p, :o, 'user', 'active', 'shared') ON CONFLICT (id) DO NOTHING"),
                   {"p": LEGACY_SHARED_PRINCIPAL, "o": w.org})
        issued = sessions.issue(db, principal_id=LEGACY_SHARED_PRINCIPAL, org_id=w.org, methods=["legacy_password"],
                                mfa_at=None, ip=None, user_agent=None, device_id_hash=None)
    client, _ = w.client()
    r = w.http.get(f"/oidc/{w.org}/authorize", params={
        "response_type": "code", "client_id": client["id"], "redirect_uri": REDIRECT, "scope": "openid",
        "nonce": "n", "code_challenge": pkce()[1], "code_challenge_method": "S256"},
        headers={"Cookie": f"__Host-boss_session={issued.token}"}, follow_redirects=False)
    assert r.status_code == 403 and "location" not in r.headers


def test_a14_require_mfa_refuses_a_session_without_a_second_factor(w):
    weak = w.person("weak", mfa=False, methods=("password",))
    client, _ = w.client(require_mfa=True)
    r, _ = w.authorize(client["id"], who=weak)
    assert r.status_code == 403 and "location" not in r.headers
    r, _ = w.authorize(client["id"], who=weak, extra={"prompt": "none"})
    assert query_of(r.headers["location"])["error"] == "interaction_required"


# ---- A15 open redirect at logout -----------------------------------------------------------------

@pytest.mark.parametrize("target", ["https://evil.test/", LOGOUT + "x", LOGOUT + "/../evil", REDIRECT])
def test_a15_logout_never_redirects_to_an_unregistered_address(w, target):
    _, client, _, tok, _ = _tokens(w)
    for params in ({"id_token_hint": tok["id_token"], "post_logout_redirect_uri": target},
                   {"client_id": client["id"], "post_logout_redirect_uri": target},
                   {"post_logout_redirect_uri": target}):
        _no_redirect(w.http.get(f"/oidc/{w.org}/end_session", params=params, follow_redirects=False))


def test_a15_logout_without_a_hint_ends_nothing(w):
    _, client, _, tok, _ = _tokens(w)
    r = w.http.get(f"/oidc/{w.org}/end_session", params={"client_id": client["id"], "post_logout_redirect_uri": LOGOUT},
                   follow_redirects=False)
    assert r.status_code == 302
    assert w.userinfo(tok["access_token"]).status_code == 200


# ---- A16 what BOSS never fetches, never accepts twice ----------------------------------------------

@pytest.mark.parametrize("extra,error", [
    ({"request_uri": "https://evil.test/req"}, "request_uri_not_supported"),
    ({"request": "eyJhbGciOiJub25lIn0.e30."}, "request_not_supported"),
    ({"registration": "{}"}, "registration_not_supported"),
    ({"response_type": "code id_token"}, "unsupported_response_type"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"response_mode": "fragment"}, "invalid_request"),
    ({"scope": "email"}, "invalid_scope"),
    ({"scope": "openid admin"}, "invalid_scope"),
    ({"prompt": "none login"}, "invalid_request"),
    ({"max_age": "-1"}, "invalid_request"),
])
def test_a16_unsupported_or_dangerous_parameters_are_refused_by_redirect(w, extra, error):
    alice = w.person("alice")
    client, _ = w.client()
    r, _ = w.authorize(client["id"], who=alice, extra=extra)
    assert r.status_code == 302 and query_of(r.headers["location"])["error"] == error


def test_a16_a_scope_the_client_did_not_register_is_refused(w):
    alice = w.person("alice")
    client, _ = w.client(allowed_scopes=["openid", "email"])
    r, _ = w.authorize(client["id"], who=alice, extra={"scope": "openid email roles"})
    assert query_of(r.headers["location"])["error"] == "invalid_scope"


def test_a16_a_repeated_token_parameter_is_refused(w):
    _, client, secret, code, verifier, _ = _signed_in(w)
    body = (f"grant_type=authorization_code&code={code}&code={code}&redirect_uri={quote(REDIRECT, safe='')}"
            f"&code_verifier={verifier}")
    r = w.http.post(f"/oidc/{w.org}/token", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Authorization": _basic(client, secret)})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    assert w.exchange(client, secret, code, verifier).status_code == 200


def test_a16_the_token_endpoint_wants_a_form(w):
    _, client, secret, code, verifier, _ = _signed_in(w)
    r = w.http.post(f"/oidc/{w.org}/token", json={"grant_type": "authorization_code", "code": code},
                    headers={"Authorization": _basic(client, secret)})
    assert r.status_code == 400


_ = ORIGIN
