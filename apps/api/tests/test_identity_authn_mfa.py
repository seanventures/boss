"""Stream S1: TOTP, recovery codes, passkeys and step-up through the real app (IDENTITY.md §4.4–§4.7).

Adversarial ids (§15.3): A4, A5, A6, A26 (at the AuthContext/route level), plus J3/J5 parts. A SOFT
AUTHENTICATOR (ES256, `none` attestation) plays the browser's passkey; every check it exercises
fails when its protection is removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
from typing import Optional

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    totp_now,
    world,
)
from boss_engine.identity import api_keys, authn, ratelimit, users
from boss_engine.identity.authn_support import b64url
from boss_engine.identity.principal import AuthContext, AuthMethod

RP_ID = "boss.example.test"


# ---- the soft authenticator -------------------------------------------------------------------------

class SoftAuthenticator:
    """One discoverable ES256 credential, as a platform authenticator would hold it."""

    def __init__(self, user_handle: bytes, *, uv: bool = True, sign_count: int = 0):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle = user_handle
        self.uv = uv
        self.sign_count = sign_count

    def _cose(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode()

    def register(self, options: dict, *, origin: str = ORIGIN) -> dict:
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = (hashlib.sha256(RP_ID.encode()).digest() + bytes([self._flags(True)])
                     + struct.pack(">I", self.sign_count) + b"\0" * 16
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64url(self.credential_id), "rawId": b64url(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client_data), "attestationObject": b64url(att),
                             "transports": ["internal"]},
                "clientExtensionResults": {}, "authenticatorAttachment": "platform"}

    def assert_(self, options: dict, *, origin: str = ORIGIN, user_handle: Optional[bytes] = None,
                sign_count: Optional[int] = None) -> dict:
        self.sign_count = self.sign_count + 1 if sign_count is None else sign_count
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = (hashlib.sha256(RP_ID.encode()).digest() + bytes([self._flags(False)])
                     + struct.pack(">I", self.sign_count))
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        handle = self.user_handle if user_handle is None else user_handle
        return {"id": b64url(self.credential_id), "rawId": b64url(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client_data), "authenticatorData": b64url(auth_data),
                             "signature": b64url(signature), "userHandle": b64url(handle)},
                "clientExtensionResults": {}}


# ---- helpers ---------------------------------------------------------------------------------------

def _session(client) -> str:
    return client.cookies.get(authn.SESSION_COOKIE) or ""


def _enrol_totp(client) -> tuple[str, list[str]]:
    begin = client.post("/v1/identity/me/mfa/totp", headers=csrf(client))
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]
    confirm = client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                          headers=csrf(client))
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json().get("recovery_codes", [])


def _register_passkey(client, auth: SoftAuthenticator, name: str = "Laptop"):
    options = client.post("/v1/identity/me/passkeys/options", headers=csrf(client))
    assert options.status_code == 200, options.text
    return client.post("/v1/identity/me/passkeys", headers=csrf(client),
                       json={"credential": auth.register(options.json()["publicKey"]), "name": name})


def _passkey_sign_in(client, auth: SoftAuthenticator, **kw):
    options = client.post("/v1/auth/webauthn/options", json={"purpose": "login"}, headers=HEADERS)
    assert options.status_code == 200, options.text
    return client.post("/v1/auth/webauthn/verify", headers=HEADERS,
                       json={"purpose": "login", "credential": auth.assert_(options.json()["publicKey"], **kw)})


def _age_mfa(engine, client, minutes: int = 11) -> None:
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET mfa_at = now() - make_interval(mins => :m), "
                        "created_at = now() - make_interval(mins => :m) WHERE token_hash = :h"),
                   {"m": minutes, "h": authn.hash_token(_session(client))})


def _person_with_totp(engine, email: str, role: str = "operator"):
    pid = make_user(engine, email, role=role)
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    secret, codes = _enrol_totp(client)
    return pid, client, secret, codes


# ---- TOTP ------------------------------------------------------------------------------------------

def test_totp_enrolment_shows_the_secret_once_stores_none_and_rotates_to_fresh_mfa(world, engine):
    pid = make_user(engine, "ava@example.test")
    client = fresh_client()
    sign_in(client, "ava@example.test")
    before = _session(client)
    secret, codes = _enrol_totp(client)
    assert len(codes) == 10 and all(len(c) == 14 for c in codes)          # first factor: codes once
    after = _session(client)
    assert after and after != before                                      # rotated
    me = client.get("/v1/identity/me").json()
    assert me["sign_in"]["totp"] is True and me["mfa_at"] and secret not in json.dumps(me)
    stale = fresh_client()
    stale.cookies.set(authn.SESSION_COOKIE, before)
    assert stale.get("/v1/identity/me").status_code == 401
    with engine.begin() as db:                                            # never stored outside the vault
        for table in ("identity_totp", "identity_auth_challenges", "identity_recovery_codes"):
            dump = json.dumps([dict(r._mapping) for r in db.execute(text(f"SELECT * FROM {table}"))], default=str)
            assert secret not in dump and codes[0].replace("-", "") not in dump
    assert world.events_of("mfa.enrolled") and world.events_of("recovery_codes.generated")
    # a second confirm with no pending enrolment does nothing
    assert client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret, offset_steps=1)},
                       headers=csrf(client)).status_code == 400
    assert pid


def test_totp_enrolment_needs_a_recent_sign_in(world, engine):
    make_user(engine, "bo@example.test")
    client = fresh_client()
    sign_in(client, "bo@example.test")
    _age_mfa(engine, client)
    res = client.post("/v1/identity/me/mfa/totp", headers=csrf(client))
    assert res.status_code == 401 and res.json() == {"error": "step_up_required"}


def test_j3_password_then_totp_signs_in(world, engine):
    _, _, secret, _ = _person_with_totp(engine, "cy@example.test")
    client = fresh_client()
    first = sign_in(client, "cy@example.test")
    assert first.status_code == 200 and first.json()["next"] == "mfa"
    assert "totp" in first.json()["methods"] and not _session(client)       # no session yet
    done = client.post("/v1/auth/mfa/totp", json={"code": totp_now(secret, offset_steps=1)}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["next"] == "done", done.text
    me = client.get("/v1/identity/me").json()
    assert me["mfa_at"]
    with engine.begin() as db:
        methods = db.execute(text("SELECT auth_methods FROM identity_sessions WHERE token_hash = :h"),
                             {"h": authn.hash_token(_session(client))}).scalar()
    assert set(methods) == {"pwd", "totp"}


def test_a4_the_sixth_attempt_in_one_challenge_is_refused(world, engine):
    _, _, secret, _ = _person_with_totp(engine, "di@example.test")
    client = fresh_client()
    sign_in(client, "di@example.test")
    right = totp_now(secret, offset_steps=1)
    wrong = f"{(int(right) + 1) % 1_000_000:06d}"
    for _ in range(5):
        res = client.post("/v1/auth/mfa/totp", json={"code": wrong}, headers=HEADERS)
        assert res.status_code == 401 and res.json()["error"] == "invalid_code"
    sixth = client.post("/v1/auth/mfa/totp", json={"code": right}, headers=HEADERS)
    assert sixth.status_code == 401 and sixth.json()["error"] == "mfa_challenge_invalid"
    assert not _session(client)
    assert len(world.events_of("auth.mfa.failed")) == 5


def test_a5_a_code_is_accepted_once_even_in_a_race(world, engine):
    _, _, secret, _ = _person_with_totp(engine, "ed@example.test")
    code = totp_now(secret, offset_steps=1)
    a, b = fresh_client(), fresh_client()
    assert sign_in(a, "ed@example.test").json()["next"] == "mfa"
    assert sign_in(b, "ed@example.test").json()["next"] == "mfa"
    results: list[int] = []
    barrier = threading.Barrier(2)

    def submit(client):
        barrier.wait()
        results.append(client.post("/v1/auth/mfa/totp", json={"code": code}, headers=HEADERS).status_code)

    threads = [threading.Thread(target=submit, args=(c,)) for c in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 401], results
    # and replayed later, in a fresh challenge
    c = fresh_client()
    sign_in(c, "ed@example.test")
    assert c.post("/v1/auth/mfa/totp", json={"code": code}, headers=HEADERS).status_code == 401


def test_mfa_is_limited_to_20_per_hour_per_person(world, engine):
    pid, _, secret, _ = _person_with_totp(engine, "fi@example.test")
    with engine.begin() as db:
        for _ in range(20):
            ratelimit.hit("mfa.user", pid, limit=20, window_seconds=3600, db=db)
    client = fresh_client()
    sign_in(client, "fi@example.test")
    res = client.post("/v1/auth/mfa/totp", json={"code": totp_now(secret, offset_steps=1)}, headers=HEADERS)
    assert res.status_code == 429 and int(res.headers["Retry-After"]) >= 1


# ---- recovery codes ----------------------------------------------------------------------------------

def test_j5_a_recovery_code_signs_in_once_notifies_and_forces_regeneration_below_three(world, engine):
    world.smtp = True
    pid, _, _, codes = _person_with_totp(engine, "gil@example.test")
    client = fresh_client()
    sign_in(client, "gil@example.test")
    res = client.post("/v1/auth/mfa/recovery", json={"code": codes[0].lower()}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["recovery_codes_remaining"] == 9 and res.json()["must_regenerate_recovery_codes"] is False
    subject, _ = world.last_mail_to("gil@example.test")
    assert "recovery code" in subject.lower()
    assert world.events_of("recovery_code.used")
    again = fresh_client()
    sign_in(again, "gil@example.test")
    assert again.post("/v1/auth/mfa/recovery", json={"code": codes[0]}, headers=HEADERS).status_code == 401
    with engine.begin() as db:   # leave exactly three unused codes
        db.execute(text("UPDATE identity_recovery_codes SET used_at = now() WHERE id IN (SELECT id FROM "
                        "identity_recovery_codes WHERE principal_id = :p AND used_at IS NULL ORDER BY id LIMIT 6)"),
                   {"p": pid})
        left = [r[0] for r in db.execute(text("SELECT code_hash FROM identity_recovery_codes WHERE principal_id = :p "
                                              "AND used_at IS NULL"), {"p": pid})]
    from boss_engine.identity.passwords import verify_code

    usable = next(c for c in codes[1:] if any(verify_code(c.replace("-", ""), h) for h in left))
    third = fresh_client()
    sign_in(third, "gil@example.test")
    res = third.post("/v1/auth/mfa/recovery", json={"code": usable}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["recovery_codes_remaining"] == 2
    assert res.json()["must_regenerate_recovery_codes"] is True


def test_regenerating_recovery_codes_needs_step_up(world, engine):
    _, client, _, _ = _person_with_totp(engine, "hu@example.test")
    _age_mfa(engine, client)
    assert client.post("/v1/identity/me/recovery-codes", headers=csrf(client)).status_code == 401
    promote_fresh_mfa(engine, _session(client))
    res = client.post("/v1/identity/me/recovery-codes", headers=csrf(client))
    assert res.status_code == 200 and len(res.json()["recovery_codes"]) == 10


# ---- passkeys ------------------------------------------------------------------------------------------

def test_passkey_registration_requires_user_verification(world, engine):
    pid = make_user(engine, "ike@example.test")
    client = fresh_client()
    sign_in(client, "ike@example.test")
    res = _register_passkey(client, SoftAuthenticator(pid.encode(), uv=False))
    assert res.status_code == 400 and res.json()["error"] == "passkey_verification_failed"
    ok = _register_passkey(client, SoftAuthenticator(pid.encode()))
    assert ok.status_code == 201 and len(ok.json()["recovery_codes"]) == 10


def test_a_passkey_alone_signs_in_and_satisfies_mfa(world, engine):
    pid = make_user(engine, "jan@example.test", role="owner")
    client = fresh_client()
    sign_in(client, "jan@example.test")
    auth = SoftAuthenticator(pid.encode())
    assert _register_passkey(client, auth).status_code == 201
    browser = fresh_client()
    res = _passkey_sign_in(browser, auth)
    assert res.status_code == 200 and res.json()["next"] == "done", res.text
    me = browser.get("/v1/identity/me").json()
    assert me["principal_id"] == pid and me["mfa_at"]
    assert world.events_of("auth.passkey.succeeded")


def test_a_passkey_is_the_mfa_step_after_a_password(world, engine):
    pid = make_user(engine, "kai@example.test")
    client = fresh_client()
    sign_in(client, "kai@example.test")
    auth = SoftAuthenticator(pid.encode())
    _register_passkey(client, auth)
    browser = fresh_client()
    first = sign_in(browser, "kai@example.test")
    assert first.json()["next"] == "mfa" and "passkey" in first.json()["methods"]
    options = browser.post("/v1/auth/webauthn/options", json={"purpose": "mfa"}, headers=HEADERS)
    assert options.status_code == 200
    allowed = [c["id"] for c in options.json()["publicKey"]["allowCredentials"]]
    assert allowed == [b64url(auth.credential_id)]
    res = browser.post("/v1/auth/webauthn/verify", headers=HEADERS,
                       json={"purpose": "mfa", "credential": auth.assert_(options.json()["publicKey"])})
    assert res.status_code == 200 and res.json()["next"] == "done", res.text


def test_a_passkey_step_up_rotates_the_session(world, engine):
    pid = make_user(engine, "lea@example.test")
    client = fresh_client()
    sign_in(client, "lea@example.test")
    auth = SoftAuthenticator(pid.encode())
    _register_passkey(client, auth)
    _age_mfa(engine, client)
    old = _session(client)
    options = client.post("/v1/auth/webauthn/options", json={"purpose": "step_up"}, headers=HEADERS)
    assert options.status_code == 200
    res = client.post("/v1/auth/webauthn/verify", headers=HEADERS,
                      json={"purpose": "step_up", "credential": auth.assert_(options.json()["publicKey"])})
    assert res.status_code == 200 and res.json()["step_up_fresh_until"], res.text
    new = _session(client)
    assert new != old
    stale = fresh_client()
    stale.cookies.set(authn.SESSION_COOKIE, old)
    assert stale.get("/v1/identity/me").status_code == 401
    assert world.events_of("auth.step_up")


def test_a6_a_challenge_from_one_browser_fails_in_another(world, engine):
    pid = make_user(engine, "mo@example.test")
    owner = fresh_client()
    sign_in(owner, "mo@example.test")
    auth = SoftAuthenticator(pid.encode())
    _register_passkey(owner, auth)
    x, y = fresh_client(), fresh_client()
    x_options = x.post("/v1/auth/webauthn/options", json={"purpose": "login"}, headers=HEADERS).json()["publicKey"]
    signed_for_x = auth.assert_(x_options)
    # Y has no challenge of its own: refused
    assert y.post("/v1/auth/webauthn/verify", headers=HEADERS,
                  json={"purpose": "login", "credential": signed_for_x}).status_code == 401
    # Y has its own challenge, but presents the assertion over X's: refused
    y.post("/v1/auth/webauthn/options", json={"purpose": "login"}, headers=HEADERS)
    assert y.post("/v1/auth/webauthn/verify", headers=HEADERS,
                  json={"purpose": "login", "credential": signed_for_x}).status_code == 401
    assert not y.cookies.get(authn.SESSION_COOKIE)
    # X's own challenge is single use: the same assertion replayed in X is refused too
    first = x.post("/v1/auth/webauthn/verify", headers=HEADERS, json={"purpose": "login", "credential": signed_for_x})
    assert first.status_code == 200
    replay = fresh_client()
    replay.cookies.set(authn.PREAUTH_COOKIE, "bpa_none")
    assert replay.post("/v1/auth/webauthn/verify", headers=HEADERS,
                       json={"purpose": "login", "credential": signed_for_x}).status_code == 401


def test_a6_a_user_handle_of_someone_else_fails(world, engine):
    pid = make_user(engine, "nat@example.test")
    other = make_user(engine, "oto@example.test", role="owner")
    client = fresh_client()
    sign_in(client, "nat@example.test")
    auth = SoftAuthenticator(pid.encode())
    _register_passkey(client, auth)
    browser = fresh_client()
    res = _passkey_sign_in(browser, auth, user_handle=other.encode())
    assert res.status_code == 401 and not browser.cookies.get(authn.SESSION_COOKIE)
    assert any(e.details.get("reason") == "user_mismatch" for e in world.events_of("auth.passkey.failed"))


def test_a6_another_persons_passkey_cannot_answer_my_mfa_challenge(world, engine):
    alice = make_user(engine, "pam@example.test")
    bob = make_user(engine, "quy@example.test")
    a_client, b_client = fresh_client(), fresh_client()
    sign_in(a_client, "pam@example.test")
    sign_in(b_client, "quy@example.test")
    _register_passkey(a_client, SoftAuthenticator(alice.encode()))
    bob_key = SoftAuthenticator(bob.encode())
    _register_passkey(b_client, bob_key)
    attacker = fresh_client()
    assert sign_in(attacker, "pam@example.test").json()["next"] == "mfa"   # Alice's password, stolen
    options = attacker.post("/v1/auth/webauthn/options", json={"purpose": "mfa"}, headers=HEADERS).json()
    res = attacker.post("/v1/auth/webauthn/verify", headers=HEADERS,
                        json={"purpose": "mfa", "credential": bob_key.assert_(options["publicKey"])})
    assert res.status_code == 401 and not attacker.cookies.get(authn.SESSION_COOKIE)


def test_a_sign_count_regression_is_recorded_but_does_not_block(world, engine):
    pid = make_user(engine, "ria@example.test")
    client = fresh_client()
    sign_in(client, "ria@example.test")
    auth = SoftAuthenticator(pid.encode(), sign_count=5)
    _register_passkey(client, auth)
    res = _passkey_sign_in(fresh_client(), auth, sign_count=3)
    assert res.status_code == 200, res.text
    assert world.events_of("passkey.counter_regressed")
    with engine.begin() as db:
        assert db.execute(text("SELECT sign_count FROM identity_webauthn_credentials")).scalar() == 5


def test_ten_passkeys_at_most(world, engine):
    pid = make_user(engine, "sol@example.test")
    client = fresh_client()
    sign_in(client, "sol@example.test")
    for i in range(10):
        assert _register_passkey(client, SoftAuthenticator(pid.encode()), name=f"k{i}").status_code == 201
    res = client.post("/v1/identity/me/passkeys/options", headers=csrf(client))
    assert res.status_code == 409 and res.json()["error"] == "passkey_limit_reached"


# ---- removing factors and step-up -------------------------------------------------------------------

def test_an_owner_cannot_remove_their_last_second_factor(world, engine):
    _, client, _, _ = _person_with_totp(engine, "tom@example.test", role="owner")
    promote_fresh_mfa(engine, _session(client))
    res = client.delete("/v1/identity/me/mfa/totp", headers=csrf(client))
    assert res.status_code == 409 and res.json()["error"] == "last_factor"


def test_removing_totp_needs_step_up(world, engine):
    _, client, secret, _ = _person_with_totp(engine, "uli@example.test")
    _age_mfa(engine, client)
    res = client.delete("/v1/identity/me/mfa/totp", headers=csrf(client))
    assert res.status_code == 401 and res.json() == {"error": "step_up_required"}
    step = client.post("/v1/auth/step-up", json={"method": "totp", "code": totp_now(secret, offset_steps=1)},
                       headers=csrf(client))
    assert step.status_code == 200, step.text
    assert client.delete("/v1/identity/me/mfa/totp", headers=csrf(client)).status_code == 200
    assert world.events_of("mfa.removed")


def test_password_step_up_is_refused_to_a_person_with_a_second_factor(world, engine):
    _, client, _, _ = _person_with_totp(engine, "vic@example.test")
    res = client.post("/v1/auth/step-up", json={"method": "password", "password": STRONG}, headers=csrf(client))
    assert res.status_code == 422 and res.json()["error"] == "second_factor_required"
    plain = make_user(engine, "wyn@example.test", role="viewer")
    other = fresh_client()
    sign_in(other, "wyn@example.test")
    ok = other.post("/v1/auth/step-up", json={"method": "password", "password": STRONG}, headers=csrf(other))
    assert ok.status_code == 200 and plain


def test_a26_a_bearer_credential_can_never_step_up(world, engine):
    pid = make_user(engine, "xia@example.test", role="operator")
    with engine.begin() as db:
        owner = users.load_principal(db, pid)
        key = api_keys.create(db, creator=AuthContext(owner, AuthMethod.SESSION), owner=owner, kind="pat",
                              name="ci", scopes=["api_key:manage_own", "settings:read"]).key
        ctx = authn.authenticate(authn.RequestCredentials("POST", "/x", {"authorization": f"Bearer {key}"}, {}),
                                 db=db)
    assert ctx.session is None and ctx.method == AuthMethod.API_KEY   # nothing to be fresh with
    bearer = {"Authorization": f"Bearer {key}"}
    client = fresh_client()
    res = client.post("/v1/auth/step-up", json={"method": "password", "password": STRONG}, headers=bearer)
    assert res.status_code == 401 and res.json() == {"error": "step_up_required"}
    # a step-up permission through a PAT: refused as step_up_required, never allowed
    res = client.post("/v1/identity/me/api-keys", json={"name": "x", "scopes": ["settings:read"]}, headers=bearer)
    assert res.status_code == 401 and res.json() == {"error": "step_up_required"}
    # factor writes refuse a bearer outright
    assert client.delete("/v1/identity/me/mfa/totp", headers=bearer).status_code == 403
    assert client.post("/v1/identity/me/passkeys/options", headers=bearer).status_code == 403


@pytest.mark.parametrize("minutes", [11])
def test_step_up_freshness_is_ten_minutes(world, engine, minutes):
    _, client, _, _ = _person_with_totp(engine, "yan@example.test")
    perms = client.get("/v1/identity/me/permissions").json()
    assert perms["step_up_fresh_until"]
    _age_mfa(engine, client, minutes)
    assert client.get("/v1/identity/me/permissions").json()["step_up_fresh_until"] is None
