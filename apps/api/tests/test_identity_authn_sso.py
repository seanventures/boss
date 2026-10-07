"""Stream S1: SSO console sign-in — OIDC, GitHub, SAML — and identity-provider admin.

Adversarial ids (IDENTITY.md §15.3): A61–A71, A89, A90, A92 (SSO side). The IdP is in-test: an RSA
key, a discovery document, a JWKS and a token endpoint served by replacing sso_oidc._fetch (unknown
URLs fall through to the real SSRF-safe client, whose sockets are forbidden here). SAML responses
are signed with signxml and a self-signed certificate made per test.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    set_policy,
    sign_in,
    world,
)
from boss_engine.identity import authn, http, sso_oidc
from boss_engine.identity.http import SafeResponse

ISSUER = "https://idp.example.com"
CLIENT_ID = "boss-console"


# ---- the fake IdP ---------------------------------------------------------------------------------

class FakeIdP:
    def __init__(self, issuer: str = ISSUER):
        self.issuer = issuer
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.claims: dict[str, Any] = {}
        self.header: dict[str, Any] = {}
        self.raw_id_token: Optional[str] = None
        self.discovery_override: dict[str, Any] = {}
        self.token_requests: list[dict[str, list[str]]] = []
        self.github_user: dict[str, Any] = {"id": 4242, "login": "octo"}
        self.github_emails: list[dict[str, Any]] = []
        self.nonce: Optional[str] = None

    def jwk(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}

    def discovery(self) -> dict[str, Any]:
        return {"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/jwks",
                **self.discovery_override}

    def id_token(self) -> str:
        if self.raw_id_token is not None:
            return self.raw_id_token
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": CLIENT_ID, "sub": "subject-1", "iat": now, "exp": now + 300,
                  "nonce": self.nonce, "email": "pat@corp.test", "email_verified": True, "name": "Pat",
                  **self.claims}
        claims = {k: v for k, v in claims.items() if v is not None}
        pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": self.kid, **self.header})

    def fetch(self, real):
        def fetch(method: str, url: str, **kw: Any) -> SafeResponse:
            def j(obj: Any, status: int = 200) -> SafeResponse:
                return SafeResponse(status, {"content-type": "application/json"}, json.dumps(obj).encode())
            if url == f"{self.issuer}/.well-known/openid-configuration":
                return j(self.discovery())
            if url == f"{self.issuer}/jwks":
                return j({"keys": [self.jwk()]})
            if url in (f"{self.issuer}/token", "https://github.com/login/oauth/access_token"):
                self.token_requests.append(parse_qs((kw.get("data") or b"").decode()))
                if url.startswith("https://github.com"):
                    return j({"access_token": "gho_fake", "token_type": "bearer"})
                return j({"access_token": "at", "id_token": self.id_token(), "token_type": "Bearer"})
            if url == "https://api.github.com/user":
                return j(self.github_user)
            if url == "https://api.github.com/user/emails":
                return j(self.github_emails)
            return real(method, url, **kw)
        return fetch


@pytest.fixture
def idp(world, monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(sso_oidc, "_fetch", fake.fetch(http.fetch))
    sso_oidc.clear_caches()

    def no_sockets(*a, **k):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(http, "_open_connection", no_sockets)
    monkeypatch.setattr(http, "_resolve", lambda host, port: ["93.184.216.34"])
    yield fake
    sso_oidc.clear_caches()


def make_provider(engine, *, protocol: str = "oidc", preset: str = "generic", issuer: Optional[str] = ISSUER,
                  client_id: Optional[str] = CLIENT_ID, jit_mode: str = "off", domains: tuple = ("corp.test",),
                  trust_idp_mfa: bool = False, groups_claim: Optional[str] = None,
                  certificates: tuple = (), status: str = "active", algs: tuple = ("RS256", "ES256", "EdDSA")) -> str:
    from boss_engine.identity import ids
    from boss_engine.identity.authn_support import default_org_id

    pid = ids.new_id("idp")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO identity_providers (id, org_id, protocol, preset, name, issuer, client_id, allowed_algs, "
            "domains, jit_mode, trust_idp_mfa, groups_claim, saml_certificates, status) VALUES (:id, :o, :p, :pre, "
            ":n, :i, :c, :a, :d, :j, :t, :g, :certs, :st)"),
            {"id": pid, "o": default_org_id(db), "p": protocol, "pre": preset, "n": f"IdP {pid[-4:]}",
             "i": issuer, "c": client_id, "a": list(algs), "d": list(domains), "j": jit_mode,
             "t": trust_idp_mfa, "g": groups_claim, "certs": list(certificates), "st": status})
    return pid


def invite(engine, email: str, role: str = "operator") -> None:
    from boss_engine.identity import ids
    from boss_engine.identity.authn_support import default_org_id

    with engine.begin() as db:
        # A real, active inviter who holds what the invitation grants: accept re-checks the
        # ceiling against the inviter as they are then (C1), so a made-up inviter grants nothing.
        org = default_org_id(db)
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "('usr_admin', :o, 'user', 'active', 'Admin') ON CONFLICT (id) DO UPDATE SET "
                        "status = 'active'"), {"o": org})
        db.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, "
                        "scope_type, scope_id, granted_by) VALUES (:id, :o, 'principal', 'usr_admin', "
                        "'arl_admin', 'org', :o, 'test') ON CONFLICT DO NOTHING"),
                   {"id": ids.new_id("rbd"), "o": org})
        db.execute(text("INSERT INTO identity_invitations (id, org_id, email, token_hash, bindings, invited_by, "
                        "expires_at) VALUES (:id, :o, :e, :h, CAST(:b AS jsonb), 'usr_admin', now() + interval '7 days')"),
                   {"id": ids.new_id("inv"), "o": default_org_id(db), "e": email, "h": uuid.uuid4().hex,
                    "b": json.dumps([{"role_key": role, "scope_type": "org", "scope_id": None}])})


def counts(engine) -> dict[str, int]:
    with engine.begin() as db:
        return {t: int(db.execute(text(f"SELECT count(*) FROM {t}")).scalar()) for t in (
            "identity_users", "identity_user_identities", "identity_sessions")}


def start(client, provider_id: str, idp: FakeIdP, purpose: str = "login") -> dict[str, str]:
    res = client.get(f"/v1/auth/sso/{provider_id}/start", params={"purpose": purpose}, follow_redirects=False)
    assert res.status_code == 302, res.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(res.headers["location"]).query).items()}
    idp.nonce = q.get("nonce")
    return q


def finish(client, provider_id: str, state: str, **extra: str):
    return client.get(f"/v1/auth/sso/{provider_id}/callback", params={"code": "the-code", "state": state, **extra},
                      follow_redirects=False)


def login_via(client, provider_id: str, idp: FakeIdP, **extra: str):
    q = start(client, provider_id, idp)
    return finish(client, provider_id, q["state"], **extra), q


def assert_refused(res, code: Optional[str] = None) -> None:
    assert res.status_code == 303, res.text
    loc = res.headers["location"]
    assert loc.startswith(f"{ORIGIN}/signin?error="), loc
    if code:
        assert loc.endswith(f"error={code}"), loc
    assert not any(c.startswith(authn.SESSION_COOKIE + "=bss_") for c in res.headers.get_list("set-cookie"))


def assert_signed_in(res) -> None:
    assert res.status_code == 303, res.text
    assert res.headers["location"] == f"{ORIGIN}/", res.headers["location"]
    assert any(c.startswith(authn.SESSION_COOKIE + "=bss_") for c in res.headers.get_list("set-cookie"))


# ---- OIDC: happy path, A92 ----------------------------------------------------------------------------

def test_an_invited_person_signs_in_by_sso_bound_by_issuer_and_subject(world, engine, idp):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    client = fresh_client()
    res, q = login_via(client, pid, idp)
    assert_signed_in(res)
    # PKCE: the token call carried the verifier whose S256 is the challenge sent at start (A64)
    verifier = idp.token_requests[-1]["code_verifier"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"] == expected and q["code_challenge_method"] == "S256"
    assert idp.token_requests[-1]["redirect_uri"] == [f"{ORIGIN}/v1/auth/sso/{pid}/callback"]
    with engine.begin() as db:
        link = db.execute(text("SELECT issuer, subject FROM identity_user_identities")).one()
    assert tuple(link) == (ISSUER, "subject-1")
    me = client.get("/v1/identity/me").json()
    assert me["email"] == "pat@corp.test" and me["access_roles"] == ["operator"]
    # A92: nothing secret in the redirect or the body, and the start URL holds no session token
    assert res.content == b"" or b"bss_" not in res.content
    for value in (res.headers["location"],):
        assert "code" not in value and "state" not in value and "bss_" not in value and "token" not in value
    assert world.events_of("sso.login") and world.events_of("sso.link")
    # a second sign-in uses the link: no new user
    before = counts(engine)["identity_users"]
    res2, _ = login_via(fresh_client(), pid, idp)
    assert_signed_in(res2)
    assert counts(engine)["identity_users"] == before


# ---- A61 / A89: never by email -------------------------------------------------------------------------

@pytest.mark.parametrize("verified", [True, False])
def test_a61_a89_an_idp_asserting_an_existing_accounts_email_links_nothing(world, engine, idp, verified):
    make_user(engine, "victim@corp.test", role="owner")
    pid = make_provider(engine, jit_mode="domain", domains=("corp.test",))
    set_policy(engine, signup_enabled=True)
    idp.claims = {"email": "victim@corp.test", "email_verified": verified, "sub": "attacker-sub"}
    before = counts(engine)
    res, _ = login_via(fresh_client(), pid, idp)
    assert_refused(res, "not_invited")
    assert counts(engine) == before


def test_a90_an_unknown_subject_creates_nothing_without_invitation_jit_or_signup(world, engine, idp):
    pid = make_provider(engine)
    idp.claims = {"email": "stranger@else.test", "sub": "s-unknown"}
    before = counts(engine)
    assert_refused(login_via(fresh_client(), pid, idp)[0], "not_invited")
    assert counts(engine) == before
    # an unverified email never qualifies, even with JIT on for its domain (one provider per issuer,
    # F3/M1: the same provider, switched to JIT)
    jit = pid
    with engine.begin() as db:
        db.execute(text("UPDATE identity_providers SET jit_mode = 'domain', domains = '{else.test}' "
                        "WHERE id = :p"), {"p": jit})
    idp.claims = {"email": "stranger@else.test", "email_verified": False, "sub": "s-unknown"}
    assert_refused(login_via(fresh_client(), jit, idp)[0], "not_invited")
    assert counts(engine) == before


def test_a90_jit_domain_and_self_signup_create_a_person_with_no_access_roles(world, engine, idp):
    jit = make_provider(engine, jit_mode="domain", domains=("corp.test",))
    idp.claims = {"email": "new@corp.test", "sub": "s-jit"}
    client = fresh_client()
    assert_signed_in(login_via(client, jit, idp)[0])
    assert client.get("/v1/identity/me").json()["access_roles"] == []
    plain = jit           # one provider per issuer (F3/M1): the same provider, JIT off
    with engine.begin() as db:
        db.execute(text("UPDATE identity_providers SET jit_mode = 'off' WHERE id = :p"), {"p": plain})
    set_policy(engine, signup_enabled=True, signup_domains=["other.test"])
    idp.claims = {"email": "outsider@nope.test", "sub": "s-out"}
    assert_refused(login_via(fresh_client(), plain, idp)[0], "not_invited")
    idp.claims = {"email": "joiner@other.test", "sub": "s-join"}
    client = fresh_client()
    assert_signed_in(login_via(client, plain, idp)[0])
    with engine.begin() as db:
        assert db.execute(text("SELECT self_signed_up FROM identity_users WHERE email = 'joiner@other.test'")).scalar()
    assert client.get("/v1/identity/me").json()["access_roles"] == []


# ---- A62 / A63: the id_token ------------------------------------------------------------------------------

def _b64(obj: Any) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _claims(idp: FakeIdP) -> dict[str, Any]:
    now = int(time.time())
    return {"iss": ISSUER, "aud": CLIENT_ID, "sub": "subject-1", "iat": now, "exp": now + 300,
            "nonce": idp.nonce, "email": "pat@corp.test", "email_verified": True}


@pytest.mark.parametrize("forge", ["alg_none", "hs256_with_public_key", "other_key", "tampered_payload"])
def test_a62_unsigned_confused_or_forged_id_tokens_are_refused(world, engine, idp, forge):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    client = fresh_client()
    q = start(client, pid, idp)
    claims = _claims(idp)
    if forge == "alg_none":
        idp.raw_id_token = f"{_b64({'alg': 'none', 'kid': 'k1'})}.{_b64(claims)}."
    elif forge == "hs256_with_public_key":
        public_pem = idp.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
        signing_input = f"{_b64({'alg': 'HS256', 'kid': 'k1'})}.{_b64(claims)}"
        sig = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
        idp.raw_id_token = f"{signing_input}.{_b64(sig)}"
    elif forge == "other_key":
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        idp.raw_id_token = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "k1"})
    else:
        good = jwt.encode(claims, idp.key, algorithm="RS256", headers={"kid": "k1"})
        head, _payload, sig = good.split(".")
        idp.raw_id_token = f"{head}.{_b64({**claims, 'sub': 'someone-else'})}.{sig}"
    before = counts(engine)
    assert_refused(finish(client, pid, q["state"]))
    assert counts(engine) == before


@pytest.mark.parametrize("claims, code", [
    ({"aud": "someone-else"}, "audience_mismatch"),
    ({"aud": [CLIENT_ID, "other-app"]}, "azp_missing"),
    ({"aud": [CLIENT_ID, "other-app"], "azp": "other-app"}, "azp_mismatch"),
    ({"nonce": "not-the-nonce"}, "nonce_mismatch"),
    ({"iss": "https://evil.example"}, "issuer_mismatch"),
    ({"exp": int(time.time()) - 3600}, "id_token_invalid"),
])
def test_a63_wrong_audience_missing_azp_wrong_nonce_wrong_issuer_and_expiry_are_refused(world, engine, idp, claims, code):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    client = fresh_client()
    q = start(client, pid, idp)
    idp.claims = claims
    before = counts(engine)
    assert_refused(finish(client, pid, q["state"]), code)
    assert counts(engine) == before


def test_a62_an_unknown_kid_and_a_jku_header_are_never_followed(world, engine, idp):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    client = fresh_client()
    q = start(client, pid, idp)
    idp.header = {"kid": "attacker-kid", "jku": "https://evil.example/jwks"}
    assert_refused(finish(client, pid, q["state"]), "unknown_key")


def test_a62_an_alg_the_provider_does_not_allow_is_refused(world, engine, idp):
    pid = make_provider(engine, algs=("ES256",))
    invite(engine, "pat@corp.test")
    client = fresh_client()
    q = start(client, pid, idp)
    assert_refused(finish(client, pid, q["state"]), "alg_not_allowed")


# ---- A64 / A65 / A69: state, verifier, mix-up ------------------------------------------------------------

def test_a64_no_code_exchange_without_a_pkce_verifier(world, engine, idp):
    from boss_engine.identity.authn_support import create_challenge, random_token

    pid = make_provider(engine)
    preauth, state = random_token("bpa_"), random_token()
    with engine.begin() as db:
        create_challenge(db, kind="oidc_state", binding_secret=preauth, ttl_seconds=600, secret=state,
                         payload={"nonce": "n", "provider_id": pid, "purpose": "login"})   # no verifier
    client = fresh_client()
    client.cookies.set(authn.PREAUTH_COOKIE, preauth)
    assert_refused(finish(client, pid, state), "verifier_missing")
    assert idp.token_requests == []


def test_a65_provider_as_state_presented_at_provider_bs_callback_is_refused(world, engine, idp):
    a = make_provider(engine)
    b = make_provider(engine, issuer="https://idp-b.example.com")   # one provider per issuer (F3/M1)
    invite(engine, "pat@corp.test")
    client = fresh_client()
    q = start(client, a, idp)
    assert_refused(finish(client, b, q["state"]), "provider_mismatch")
    assert idp.token_requests == []


def test_a65_rfc9207_iss_and_a_discovery_issuer_mismatch_are_refused(world, engine, idp):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    client = fresh_client()
    q = start(client, pid, idp)
    assert_refused(finish(client, pid, q["state"], iss="https://other-idp.example"), "issuer_mismatch")
    assert idp.token_requests == []
    sso_oidc.clear_caches()
    idp.discovery_override = {"issuer": "https://other-idp.example"}
    res = fresh_client().get(f"/v1/auth/sso/{pid}/start", follow_redirects=False)
    assert_refused(res, "issuer_mismatch")


def test_a69_a_state_from_another_browser_is_refused_and_state_is_single_use(world, engine, idp):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    victim, attacker = fresh_client(), fresh_client()
    q = start(attacker, pid, idp)
    before = counts(engine)
    assert_refused(finish(victim, pid, q["state"]), "state_invalid")       # login CSRF
    assert counts(engine) == before and idp.token_requests == []
    assert_signed_in(finish(attacker, pid, q["state"]))
    assert_refused(finish(attacker, pid, q["state"]), "state_invalid")     # replay


# ---- A70 / A71: SSRF through configuration -----------------------------------------------------------------

def test_a70_a_metadata_issuer_is_refused_without_a_connection(world, engine, idp):
    pid = make_provider(engine, issuer="http://169.254.169.254/")
    res = fresh_client().get(f"/v1/auth/sso/{pid}/start", follow_redirects=False)
    assert_refused(res, "discovery_refused")


def test_a71_a_jwks_uri_on_loopback_is_refused(world, engine, idp, monkeypatch):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    idp.discovery_override = {"jwks_uri": "https://127.0.0.1/jwks"}
    client = fresh_client()
    q = start(client, pid, idp)
    assert_refused(finish(client, pid, q["state"]), "jwks_refused")
    assert counts(engine)["identity_user_identities"] == 0


# ---- groups, MFA, linking, disabled ---------------------------------------------------------------------------

def test_group_sync_maps_only_mapped_groups_and_removes_them_when_gone(world, engine, idp):
    from boss_engine.identity.authn_support import default_org_id

    pid = make_provider(engine, groups_claim="groups")
    with engine.begin() as db:
        org = default_org_id(db)
        db.execute(text("INSERT INTO identity_groups (id, org_id, key, name, source) VALUES "
                        "('grp_builders', :o, 'builders', 'Builders', 'oidc')"), {"o": org})
        db.execute(text("INSERT INTO identity_provider_group_mappings (provider_id, external_group, group_id) "
                        "VALUES (:p, 'eng', 'grp_builders')"), {"p": pid})
    invite(engine, "pat@corp.test")
    idp.claims = {"groups": ["eng", "admins-unmapped"]}
    assert_signed_in(login_via(fresh_client(), pid, idp)[0])
    with engine.begin() as db:
        rows = db.execute(text("SELECT group_id, source FROM identity_group_members")).fetchall()
    assert [tuple(r) for r in rows] == [("grp_builders", "oidc")]
    idp.claims = {"groups": []}
    assert_signed_in(login_via(fresh_client(), pid, idp)[0])
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_group_members")).scalar() == 0


def _give_passkey(engine, email: str) -> None:
    with engine.begin() as db:
        pid = db.execute(text("SELECT principal_id FROM identity_users WHERE email = :e"), {"e": email}).scalar()
        db.execute(text("INSERT INTO identity_webauthn_credentials (id, principal_id, credential_id, public_key, "
                        "name) VALUES ('pky_x', :p, :c, :k, 'key')"), {"p": pid, "c": b"cred", "k": b"pk"})


def test_trust_idp_mfa_counts_only_when_on_and_amr_says_mfa(world, engine, idp):
    # One provider per issuer (F3/M1): the same provider, with trust_idp_mfa switched on.
    plain = make_provider(engine)
    trusted = plain
    invite(engine, "pat@corp.test")
    assert_signed_in(login_via(fresh_client(), plain, idp)[0])
    _give_passkey(engine, "pat@corp.test")
    idp.claims = {"amr": ["mfa", "pwd"]}
    res, _ = login_via(fresh_client(), plain, idp)            # not trusted: BOSS MFA step
    assert res.headers["location"] == f"{ORIGIN}/signin?step=mfa"
    with engine.begin() as db:
        db.execute(text("UPDATE identity_providers SET trust_idp_mfa = true WHERE id = :p"), {"p": trusted})
    sso_oidc.clear_caches()
    client = fresh_client()
    res, _ = login_via(client, trusted, idp)                   # trusted + amr mfa: fresh MFA
    assert_signed_in(res)
    assert client.get("/v1/identity/me").json()["mfa_at"] is not None
    idp.claims = {"amr": ["pwd"]}
    res, _ = login_via(fresh_client(), trusted, idp)           # trusted but no MFA in amr
    assert res.headers["location"] == f"{ORIGIN}/signin?step=mfa"


def test_linking_needs_a_stepped_up_session_and_binds_to_that_person(world, engine, idp):
    make_user(engine, "sam@corp.test")
    other = make_user(engine, "other@corp.test")
    pid = make_provider(engine)
    client = fresh_client()
    assert sign_in(client, "sam@corp.test").status_code == 200
    res = client.get(f"/v1/auth/sso/{pid}/start", params={"purpose": "link"}, follow_redirects=False)
    assert res.status_code == 401 and res.json()["error"] == "step_up_required"
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    idp.claims = {"email": "whatever@else.test", "sub": "sam-at-idp"}
    q = start(client, pid, idp, purpose="link")
    res = finish(client, pid, q["state"])
    assert res.status_code == 303 and res.headers["location"] == f"{ORIGIN}/account?linked=1"
    with engine.begin() as db:
        owner = db.execute(text("SELECT u.email FROM identity_user_identities i JOIN identity_users u ON "
                                "u.principal_id = i.principal_id")).scalar()
    assert owner == "sam@corp.test"
    # the same IdP identity can never be linked to a second person
    with engine.begin() as db:
        db.execute(text("UPDATE identity_users SET email = email WHERE principal_id = :p"), {"p": other})
    second = fresh_client()
    assert sign_in(second, "other@corp.test").status_code == 200
    promote_fresh_mfa(engine, second.cookies.get(authn.SESSION_COOKIE))
    q = start(second, pid, idp, purpose="link")
    assert_refused(finish(second, pid, q["state"]), "already_linked")


def test_a_linked_but_disabled_person_is_refused(world, engine, idp):
    pid = make_provider(engine)
    invite(engine, "pat@corp.test")
    assert_signed_in(login_via(fresh_client(), pid, idp)[0])
    with engine.begin() as db:
        db.execute(text("UPDATE identity_principals SET status = 'disabled' WHERE kind = 'user'"))
    assert_refused(login_via(fresh_client(), pid, idp)[0], "account_disabled")


# ---- GitHub (OAuth 2.0 preset) ----------------------------------------------------------------------------------

def test_github_identity_is_the_numeric_id_and_only_a_verified_primary_email_counts(world, engine, idp):
    pid = make_provider(engine, protocol="oauth2", preset="github", issuer="https://github.com")
    invite(engine, "octo@corp.test")
    idp.github_emails = [{"email": "octo@corp.test", "primary": True, "verified": False}]
    assert_refused(login_via(fresh_client(), pid, idp)[0], "not_invited")
    idp.github_emails = [{"email": "octo@corp.test", "primary": True, "verified": True}]
    assert_signed_in(login_via(fresh_client(), pid, idp)[0])
    with engine.begin() as db:
        link = db.execute(text("SELECT issuer, subject FROM identity_user_identities")).one()
    assert tuple(link) == ("https://github.com", "4242")


# ---- provider admin --------------------------------------------------------------------------------------------

def _owner_client(engine):
    make_user(engine, "own@corp.test", role="owner")
    client = fresh_client()
    assert sign_in(client, "own@corp.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    me = client.get("/v1/identity/me").json()
    return client, {**HEADERS, "X-Boss-CSRF": me["csrf_token"]}


def test_provider_admin_never_returns_the_client_secret_and_refuses_unsafe_algs(world, engine, idp):
    client, h = _owner_client(engine)
    body = {"protocol": "oidc", "name": "Corp", "issuer": ISSUER, "client_id": CLIENT_ID,
            "client_secret": "s3cr3t-client-value", "domains": ["corp.test"]}
    res = client.post("/v1/identity/providers", json=body, headers=h)
    assert res.status_code == 201, res.text
    assert "s3cr3t-client-value" not in res.text and res.json()["has_client_secret"] is True
    assert "s3cr3t-client-value" in world.vault.values()
    listing = client.get("/v1/identity/providers")
    assert listing.status_code == 200 and "s3cr3t-client-value" not in listing.text
    assert "client_secret_vault_item_id" not in listing.text
    for bad in ({**body, "allowed_algs": ["HS256"]}, {**body, "allowed_algs": ["none"]}, {**body, "role": "owner"}):
        assert client.post("/v1/identity/providers", json=bad, headers=h).status_code == 422
    assert client.post("/v1/identity/providers", json={"protocol": "oidc", "preset": "microsoft", "name": "MS",
                                                       "client_id": "x", "tenant": "common"},
                       headers=h).status_code == 422
    pid = res.json()["id"]
    upd = client.patch(f"/v1/identity/providers/{pid}", json={"client_secret": "rotated-secret-2"}, headers=h)
    assert upd.status_code == 200 and "rotated-secret-2" not in upd.text
    assert world.events_of("sso.provider.created") and world.events_of("sso.provider.updated")


def test_an_operator_cannot_manage_providers(world, engine, idp):
    make_user(engine, "op@corp.test", role="operator")
    client = fresh_client()
    sign_in(client, "op@corp.test")
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    h = {**HEADERS, "X-Boss-CSRF": client.get("/v1/identity/me").json()["csrf_token"]}
    res = client.post("/v1/identity/providers", json={"protocol": "oidc", "name": "x", "issuer": ISSUER}, headers=h)
    assert res.status_code == 403
    assert client.get("/v1/identity/providers").status_code == 403


def test_a_scim_key_is_shown_once_and_held_by_the_providers_system_principal(world, engine, idp):
    client, h = _owner_client(engine)
    pid = make_provider(engine)
    res = client.post(f"/v1/identity/providers/{pid}/scim-keys", json={"name": "okta"}, headers=h)
    assert res.status_code == 201, res.text
    key = res.json()["key"]
    assert key.startswith("boss_scim_") and res.json()["principal_id"] == "sys_scim_" + pid.split("_", 1)[1]
    assert authn.parse_api_key(key).checksum_ok
    for path in ("/v1/identity/providers",):
        assert key not in client.get(path).text
    with engine.begin() as db:
        row = db.execute(text("SELECT scopes, token_hash FROM identity_api_credentials WHERE kind = 'scim'")).one()
    assert list(row.scopes) == ["scim:provision"] and row.token_hash == authn.hash_token(key)
    revoke = client.delete(f"/v1/identity/providers/{pid}/scim-keys/{res.json()['id']}", headers=h)
    assert revoke.status_code == 200


# ---- SAML ---------------------------------------------------------------------------------------------------------

SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
IDP_ENTITY = "https://saml-idp.example.com"
SSO_URL = "https://saml-idp.example.com/sso"
ACS = f"{ORIGIN}/saml/v2/acs"
SP_ENTITY = f"{ORIGIN}/saml/v2/metadata"


class SamlIdP:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "saml-idp")])
        now = dt.datetime.now(dt.timezone.utc)
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
                     .sign(self.key, hashes.SHA256()))
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def assertion_xml(self, *, in_response_to: str, name_id: str = "pat@corp.test", aid: Optional[str] = None,
                      audience: str = SP_ENTITY, recipient: str = ACS, not_on_or_after: Optional[str] = None,
                      email: str = "pat@corp.test") -> str:
        now = dt.datetime.now(dt.timezone.utc)
        noa = not_on_or_after or (now + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        nb = (now - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        aid = aid or "_a" + uuid.uuid4().hex
        return (f'<saml:Assertion xmlns:saml="{SAML}" ID="{aid}" Version="2.0" IssueInstant="{nb}">'
                f'<saml:Issuer>{IDP_ENTITY}</saml:Issuer>'
                f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">'
                f'{name_id}</saml:NameID><saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
                f'<saml:SubjectConfirmationData InResponseTo="{in_response_to}" Recipient="{recipient}" '
                f'NotOnOrAfter="{noa}"/></saml:SubjectConfirmation></saml:Subject>'
                f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{noa}"><saml:AudienceRestriction>'
                f'<saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
                f'<saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue>{email}'
                f'</saml:AttributeValue></saml:Attribute></saml:AttributeStatement></saml:Assertion>')

    def sign(self, xml: str, key=None, pem: Optional[str] = None):
        from lxml import etree
        from signxml import XMLSigner, algorithms

        el = etree.fromstring(xml.encode())
        return XMLSigner(c14n_algorithm=algorithms.CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0).sign(
            el, key=key or self.key, cert=pem or self.pem, reference_uri="#" + el.get("ID"))

    @staticmethod
    def response(*assertions, in_response_to: Optional[str] = None, destination: str = ACS, extra: str = ""):
        from lxml import etree

        irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
        root = etree.fromstring(
            f'<samlp:Response xmlns:samlp="{SAMLP}" xmlns:saml="{SAML}" ID="_r{uuid.uuid4().hex}" Version="2.0" '
            f'IssueInstant="2026-01-01T00:00:00Z" Destination="{destination}"{irt}>'
            f'<saml:Issuer>{IDP_ENTITY}</saml:Issuer><samlp:Status><samlp:StatusCode '
            f'Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>{extra}</samlp:Response>'.encode())
        for a in assertions:
            root.append(a if not isinstance(a, str) else etree.fromstring(a.encode()))
        return root

    @staticmethod
    def encode(root) -> str:
        from lxml import etree

        return base64.b64encode(etree.tostring(root)).decode()


@pytest.fixture
def saml(world, engine):
    idp = SamlIdP()
    pid = make_provider(engine, protocol="saml", issuer=IDP_ENTITY, client_id=SSO_URL, certificates=(idp.pem,))
    return idp, pid


def saml_start(client, pid: str) -> str:
    import zlib

    res = client.get(f"/v1/auth/sso/{pid}/start", follow_redirects=False)
    assert res.status_code == 302, res.text
    cookie = [c for c in res.headers.get_list("set-cookie") if c.startswith(authn.PREAUTH_COOKIE)][0]
    assert "SameSite=None" in cookie and "Secure" in cookie and "HttpOnly" in cookie
    q = parse_qs(urlsplit(res.headers["location"]).query)
    xml = zlib.decompress(base64.b64decode(q["SAMLRequest"][0]), -15).decode()
    return xml.split(' ID="', 1)[1].split('"', 1)[0]


def post_acs(client, payload: str):
    return client.post("/saml/v2/acs", data={"SAMLResponse": payload}, follow_redirects=False)


def test_saml_sp_initiated_sign_in_binds_entity_id_and_name_id(saml, engine):
    idp, pid = saml
    invite(engine, "pat@corp.test")
    client = fresh_client()
    rid = saml_start(client, pid)
    signed = idp.sign(idp.assertion_xml(in_response_to=rid))
    res = post_acs(client, idp.encode(idp.response(signed, in_response_to=rid)))
    assert_signed_in(res)
    with engine.begin() as db:
        link = db.execute(text("SELECT issuer, subject FROM identity_user_identities")).one()
    assert tuple(link) == (IDP_ENTITY, "pat@corp.test")


def _xsw_variants(idp: SamlIdP, rid: str):
    """The canonical XML-signature-wrapping layouts (Somorovsky et al.), plus forgeries."""
    from lxml import etree

    good = idp.sign(idp.assertion_xml(in_response_to=rid))
    good_id = good.get("ID")
    evil_xml = idp.assertion_xml(in_response_to=rid, name_id="admin@corp.test", email="admin@corp.test")
    ds = "{http://www.w3.org/2000/09/xmldsig#}"

    def evil(same_id: bool = False, with_sig: bool = False):
        e = etree.fromstring(evil_xml.encode())
        if same_id:
            e.set("ID", good_id)
        if with_sig:
            e.append(copy.deepcopy(good.find(f"{ds}Signature")))
        return e

    # 1-2: evil assertion before / after the signed one
    yield "evil_first", idp.response(evil(), copy.deepcopy(good), in_response_to=rid)
    yield "evil_after", idp.response(copy.deepcopy(good), evil(), in_response_to=rid)
    # 3: signed assertion hidden in Extensions, evil at top level with the same ID
    r = idp.response(evil(same_id=True, with_sig=True), in_response_to=rid)
    ext = etree.SubElement(r, f"{{{SAMLP}}}Extensions")
    ext.append(copy.deepcopy(good))
    yield "signed_in_extensions", r
    # 4: signed assertion wrapped inside the evil one (Advice)
    e = evil(same_id=True, with_sig=True)
    advice = etree.SubElement(e, f"{{{SAML}}}Advice")
    advice.append(copy.deepcopy(good))
    yield "signed_in_advice", idp.response(e, in_response_to=rid)
    # 5: signed assertion moved into the Signature's Object, evil carries the signature
    e = evil(same_id=True, with_sig=True)
    sig = e.find(f"{ds}Signature")
    obj = etree.SubElement(sig, f"{ds}Object")
    stripped = copy.deepcopy(good)
    stripped.remove(stripped.find(f"{ds}Signature"))
    obj.append(stripped)
    yield "signed_in_signature_object", idp.response(e, in_response_to=rid)
    # 6: evil assertion only, with the good signature copied in (ID collision)
    yield "copied_signature", idp.response(evil(same_id=True, with_sig=True), in_response_to=rid)
    # 7: the signed assertion with its NameID changed after signing
    t = copy.deepcopy(good)
    t.find(f".//{{{SAML}}}NameID").text = "admin@corp.test"
    yield "tampered_name_id", idp.response(t, in_response_to=rid)
    # 8: signature stripped
    u = copy.deepcopy(good)
    u.remove(u.find(f"{ds}Signature"))
    yield "unsigned", idp.response(u, in_response_to=rid)
    # 9: signed by another key with its own certificate
    other = SamlIdP()
    yield "attacker_cert", idp.response(other.sign(evil_xml), in_response_to=rid)
    # 10: comment truncation inside the signed NameID
    commented = idp.assertion_xml(in_response_to=rid, name_id="admin@corp.test<!---->.evil.test")
    yield "comment_in_name_id", idp.response(idp.sign(commented), in_response_to=rid)


@pytest.mark.parametrize("variant", ["evil_first", "evil_after", "signed_in_extensions", "signed_in_advice",
                                     "signed_in_signature_object", "copied_signature", "tampered_name_id",
                                     "unsigned", "attacker_cert", "comment_in_name_id"])
def test_a66_xml_signature_wrapping_variants_are_rejected(saml, engine, variant):
    idp, pid = saml
    invite(engine, "admin@corp.test")
    invite(engine, "pat@corp.test")
    client = fresh_client()
    rid = saml_start(client, pid)
    root = dict(_xsw_variants(idp, rid))[variant]
    before = counts(engine)
    assert_refused(post_acs(client, idp.encode(root)))
    assert counts(engine) == before


def test_a67_a_replayed_assertion_id_is_rejected(saml, engine):
    idp, pid = saml
    invite(engine, "pat@corp.test")
    client = fresh_client()
    rid1 = saml_start(client, pid)
    assert_signed_in(post_acs(client, idp.encode(idp.response(idp.sign(idp.assertion_xml(
        in_response_to=rid1, aid="_fixed")), in_response_to=rid1))))
    # a fresh request answered with an assertion carrying the same id: the replay cache refuses it
    other = fresh_client()
    rid2 = saml_start(other, pid)
    res = post_acs(other, idp.encode(idp.response(idp.sign(idp.assertion_xml(
        in_response_to=rid2, aid="_fixed")), in_response_to=rid2)))
    assert_refused(res, "saml_replay")
    # and the very same response posted again is refused (its request is spent)
    rid3 = saml_start(client, pid)
    del rid3
    assert_refused(post_acs(client, idp.encode(idp.response(idp.sign(idp.assertion_xml(
        in_response_to=rid1)), in_response_to=rid1))))


def test_a68_an_unsolicited_or_other_browsers_response_is_rejected(saml, engine):
    idp, pid = saml
    invite(engine, "pat@corp.test")
    client = fresh_client()
    # IdP-initiated: no InResponseTo at all
    a = idp.assertion_xml(in_response_to="").replace(' InResponseTo=""', "")
    assert_refused(post_acs(client, idp.encode(idp.response(idp.sign(a)))), "saml_unsolicited")
    # InResponseTo of a request another browser started
    starter = fresh_client()
    rid = saml_start(starter, pid)
    assert_refused(post_acs(fresh_client(), idp.encode(idp.response(
        idp.sign(idp.assertion_xml(in_response_to=rid)), in_response_to=rid))), "saml_unsolicited")
    assert counts(engine)["identity_users"] == 0


@pytest.mark.parametrize("kwargs, code", [
    ({"audience": "https://other-sp.example"}, "audience_mismatch"),
    ({"recipient": "https://evil.example/acs"}, "saml_recipient"),
    ({"not_on_or_after": "2001-01-01T00:00:00Z"}, "saml_expired"),
])
def test_saml_conditions_are_enforced(saml, engine, kwargs, code):
    idp, pid = saml
    invite(engine, "pat@corp.test")
    client = fresh_client()
    rid = saml_start(client, pid)
    root = idp.response(idp.sign(idp.assertion_xml(in_response_to=rid, **kwargs)), in_response_to=rid)
    assert_refused(post_acs(client, idp.encode(root)), code)


def test_saml_doctype_and_wrong_destination_are_rejected(saml, engine):
    idp, pid = saml
    invite(engine, "pat@corp.test")
    client = fresh_client()
    rid = saml_start(client, pid)
    from lxml import etree

    root = idp.response(idp.sign(idp.assertion_xml(in_response_to=rid)), in_response_to=rid)
    xml = b'<!DOCTYPE r [<!ENTITY x "y">]>' + etree.tostring(root)
    assert_refused(post_acs(client, base64.b64encode(xml).decode()), "saml_malformed")
    wrong = idp.response(idp.sign(idp.assertion_xml(in_response_to=rid)), in_response_to=rid,
                         destination="https://evil.example/acs")
    assert_refused(post_acs(client, idp.encode(wrong)), "saml_destination")


def test_saml_metadata_names_the_acs(world, engine):
    res = fresh_client().get("/saml/v2/metadata")
    assert res.status_code == 200 and ACS in res.text and "WantAssertionsSigned=\"true\"" in res.text

