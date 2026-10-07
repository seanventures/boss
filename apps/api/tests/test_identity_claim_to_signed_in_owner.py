"""HOTFIX (boss-1, 2026-10-07): the first Owner claims, enrols an authenticator and is SIGNED IN —
the whole flow, on both claim paths (the setup link and the shared console password), through the
real app with the real vault, audit log, policy and authorisation (only mail and the network
bridge stay faked). The S1 world fakes the vault and the audit log, so no earlier test ever ran a
claim through a real TOTP enrolment to a working Owner session.

What boss-1 showed: a shared-password claim made a session recorded as `network` (it was not
network-bridged), and the authenticator step could not be finished. Pinned here:
  * the claim's session records what the claim proved (setup_link / legacy_password, plus pwd
    when the Owner chose a password) — never `network` unless the network bridge made it;
  * begin -> confirm with a valid code enrols, rotates to a fresh-MFA session and the Owner can
    use an enforced, Owner-only route; the enrolment banner is gone;
  * a confirm with no live enrolment (the key expired, or a newer begin replaced it) is answered
    409 `enrolment_expired` — not `invalid_code`, which the console would show forever while the
    code was never even checked — and a fresh begin then enrols.
"""
from __future__ import annotations

import hashlib
import re

import pytest
from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    STRONG,
    csrf,
    fresh_client,
    totp_now,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import api_keys, audit, authn, authz, email_signin, policy, sessions, users, vault

REAL = {
    (audit, "record"): audit.record,
    (policy, "get_policy"): policy.get_policy,
    (users, "get_policy"): policy.get_policy,
    (sessions, "get_policy"): policy.get_policy,
    (email_signin, "get_policy"): policy.get_policy,
    (api_keys, "get_policy"): policy.get_policy,
    (vault, "store"): vault.store,
    (vault, "get_credential"): vault.get_credential,
    (authz, "check"): authz.check,
    (authz, "effective_permissions"): authz.effective_permissions,
    (authz, "bump_epoch"): authz.bump_epoch,
}
TEST_MASTER_KEY = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="   # b"k" * 32: the identity suites' key
SHARED = "the-old-shared-console-password"
URL_RE = re.compile(r"/claim#t=(bst_[A-Za-z0-9_\-]+)")


@pytest.fixture
def real_world(world, engine, monkeypatch):
    for (module, name), fn in REAL.items():
        monkeypatch.setattr(module, name, fn)
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("BOSS_MASTER_KEY", TEST_MASTER_KEY)
    monkeypatch.setattr(get_settings(), "legacy_console_password", SHARED)
    vault.reset_master_key_cache()
    with engine.begin() as db:      # what the API's boot does (vault.boot): register the KEK
        vault.check_master_key(db=db)
    yield world
    vault.reset_master_key_cache()


def _setup_token(capsys) -> str:
    from apps.api.app import main

    fresh_client().get("/v1/auth/setup/status")
    capsys.readouterr()
    assert main.announce_claim_link_once() is True
    m = URL_RE.search(capsys.readouterr().out)
    assert m
    return m.group(1)


def _session_row(engine, client):
    token = client.cookies.get(authn.SESSION_COOKIE)
    with engine.begin() as db:
        return db.execute(text("SELECT auth_methods, mfa_at FROM identity_sessions WHERE token_hash = :h"),
                          {"h": hashlib.sha256(token.encode()).hexdigest()}).first()


def _enrol_and_prove_signed_in(engine, client) -> None:
    me = client.get("/v1/identity/me").json()
    assert {b["kind"] for b in me["banners"]} >= {"mfa_enrolment_required"}
    # an enrolment-only session can do nothing else yet
    assert client.get("/v1/identity/users").status_code == 403
    begun = client.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(client))
    assert begun.status_code == 200, begun.text
    secret = begun.json()["secret"]
    # the QR is a standalone SVG an <img> can draw (boss-1 showed a broken image: no xmlns)
    assert 'xmlns="http://www.w3.org/2000/svg"' in begun.json()["qr_svg"]
    done = client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                       headers=csrf(client))
    assert done.status_code == 200, done.text
    assert done.json()["enrolled"] is True and len(done.json()["recovery_codes"]) == 10
    with engine.begin() as db:
        assert db.execute(text("SELECT count(*) FROM identity_totp")).scalar() == 1
        assert db.execute(text("SELECT count(*) FROM identity_security_events "
                               "WHERE event_type = 'mfa.enrolled'")).scalar() == 1
    row = _session_row(engine, client)
    assert row.mfa_at is not None and "totp" in row.auth_methods
    me = client.get("/v1/identity/me").json()
    assert "mfa_enrolment_required" not in {b["kind"] for b in me["banners"]}
    assert me["access_roles"] == ["owner"] and me["sign_in"]["totp"] is True
    # a FULLY signed-in Owner: an enforced, permission-checked route answers
    users_list = client.get("/v1/identity/users")
    assert users_list.status_code == 200, users_list.text


def test_setup_link_claim_enrols_an_authenticator_and_signs_the_owner_in(real_world, engine, capsys):
    token = _setup_token(capsys)
    client = fresh_client()
    res = client.post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "Link Owner", "email": "link@example.test", "setup_token": token, "password": STRONG})
    assert res.status_code == 200, res.text
    assert sorted(_session_row(engine, client).auth_methods) == ["pwd", "setup_link"]
    _enrol_and_prove_signed_in(engine, client)


@pytest.mark.parametrize("password", [STRONG, None], ids=["with-password", "passkey-first"])
def test_shared_password_claim_enrols_an_authenticator_and_signs_the_owner_in(real_world, engine, password):
    client = fresh_client()
    body = {"name": "Shared Owner", "email": "shared@example.test", "legacy_password": SHARED}
    if password:
        body["password"] = password
    res = client.post("/v1/auth/setup/claim", headers=HEADERS, json=body)
    assert res.status_code == 200, res.text
    methods = sorted(_session_row(engine, client).auth_methods)
    # never "network": this claim was proved by the shared password, not the network bridge
    assert methods == (["legacy_password", "pwd"] if password else ["legacy_password"])
    with engine.begin() as db:
        has_pw = db.execute(text("SELECT password_hash IS NOT NULL FROM identity_users "
                                 "WHERE email = 'shared@example.test'")).scalar()
    assert has_pw is bool(password)
    _enrol_and_prove_signed_in(engine, client)


def test_a_confirm_with_no_live_enrolment_says_so_and_a_fresh_begin_enrols(real_world, engine):
    client = fresh_client()
    assert client.post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "Slow Owner", "email": "slow@example.test", "legacy_password": SHARED,
        "password": STRONG}).status_code == 200
    secret = client.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(client)).json()["secret"]
    with engine.begin() as db:      # the operator took longer than the key lives
        db.execute(text("UPDATE identity_auth_challenges SET expires_at = now() - interval '1 second' "
                        "WHERE kind = 'mfa'"))
    stale = client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                        headers=csrf(client))
    assert stale.status_code == 409 and stale.json()["error"] == "enrolment_expired"
    # a wrong code against a LIVE key is still invalid_code, and counts
    fresh = client.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(client)).json()["secret"]
    wrong = client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret, offset_steps=7)},
                        headers=csrf(client))
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_code"
    with engine.begin() as db:
        assert db.execute(text("SELECT max(attempts) FROM identity_auth_challenges WHERE kind = 'mfa' "
                               "AND consumed_at IS NULL")).scalar() == 1
    ok = client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(fresh)}, headers=csrf(client))
    assert ok.status_code == 200, ok.text
    assert client.get("/v1/identity/users").status_code == 200
