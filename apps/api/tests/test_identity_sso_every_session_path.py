"""A-1 / A-2 (id-sec-final2): Enforce SSO holds on EVERY path that issues a session, and a host
recovery assumes compromise.

A-1 was an admin's reset link minting a full session for an SSO-enforced person: the reset path
called `signin.finish` directly and skipped the SSO check that lived only in
`finish_first_factor`. The check now lives in `finish` itself (the one place a session is issued),
and the admin reset-link refuses an SSO domain in words. A-2 was `recover-owner` leaving the
person's passkeys and API keys alive.
"""
from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401
    HEADERS, STRONG, csrf, fresh_client, make_user, mark_claimed, org_id, sign_in, totp_now, world,
)
from boss_engine.identity import cli, recovery, signin, users

ROOT = Path(__file__).resolve().parents[3]
NEW_PASSWORD = "plum-heron-quartz-4411"


def _enrolled_admin(engine, email: str):
    make_user(engine, email, role="admin")
    c = fresh_client()
    r = sign_in(c, email)
    assert r.status_code == 200 and r.json()["next"] == "enrol", r.text
    h = csrf(c)
    b = c.post("/v1/identity/me/mfa/totp", headers=h)
    assert b.status_code == 200, b.text
    ok = c.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(b.json()["secret"])}, headers=h)
    assert ok.status_code == 200, ok.text
    return c


def _enforce_sso(engine, domain: str) -> None:
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_providers (id, org_id, protocol, name, issuer, client_id, domains, "
                        "enforce_sso, status) VALUES ('idp_corp', :o, 'oidc', 'Corp', 'https://idp.corp.test', "
                        "'cid', ARRAY[:d], true, 'active')"), {"o": org_id(db), "d": domain})


def _password_hash(engine, pid: str) -> str:
    with engine.begin() as db:
        return db.execute(text("SELECT password_hash FROM identity_users WHERE principal_id = :p"),
                          {"p": pid}).scalar()


def _reset(token: str):
    c = fresh_client()
    return c, c.post("/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD},
                     headers=HEADERS)


# ---- A-1: the repro, end to end ---------------------------------------------------------------

def test_admin_reset_mfa_then_reset_link_never_signs_in_an_sso_person(world, engine):
    mark_claimed(engine)
    admin = _enrolled_admin(engine, "adm@example.test")
    vic = make_user(engine, "vic@corp.test", role="operator")
    _enforce_sso(engine, "corp.test")
    assert sign_in(fresh_client(), "vic@corp.test").status_code == 403      # control
    h = csrf(admin)
    r1 = admin.post(f"/v1/identity/users/{vic}/reset-mfa", headers=h)
    assert r1.status_code == 200, r1.text
    assert r1.json()["sso_enforced"] is True and "single sign-on" in r1.json()["message"]
    r2 = admin.post(f"/v1/identity/users/{vic}/reset-link", headers=h)
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"] == "sso_enforced"
    assert "single sign-on" in r2.json()["message"] and "link" not in r2.json()


def test_a_reset_token_for_an_sso_person_changes_nothing_and_mints_no_session(world, engine):
    """Even a reset token minted before SSO was enforced (or by any future path) is refused."""
    mark_claimed(engine)
    vic = make_user(engine, "vic2@corp.test", role="operator")
    with engine.begin() as db:
        signin.grant_first_factor_after_reset(db, vic)
        token = recovery.issue_reset_token(db, vic)
    _enforce_sso(engine, "corp.test")
    before = _password_hash(engine, vic)
    c, r = _reset(token)
    assert r.status_code == 403 and r.json()["error"] == "sso_required", r.text
    assert not any("boss_session" in v for v in r.headers.get_list("set-cookie"))
    assert _password_hash(engine, vic) == before


def test_finish_itself_refuses_a_non_sso_first_factor(world, engine):
    """The choke point: `signin.finish` (every session) refuses a password, an invitation, a
    sign-up or a claim proof for an SSO-enforced person, and admits the enforcing provider."""
    mark_claimed(engine)
    vic = make_user(engine, "vic3@corp.test", role="operator")
    _enforce_sso(engine, "corp.test")
    facts = signin.RequestFacts(ip="127.0.0.1", origin="https://boss.example.test",
                                request_origin="https://boss.example.test")
    with engine.begin() as db:
        user = users.by_id(db, vic)
        for first in ("pwd", "magic_link", "email_code", "setup_link", "network", "legacy_password",
                      "sso:idp_other", "passkey", signin.HOST_RECOVERY):
            ans = signin.finish(db, user, facts, methods=[first], mfa=False)
            assert ans.status == 403 and ans.body == {"error": "sso_required"}, (first, ans)
        ans = signin.finish(db, user, facts, methods=["pwd"], mfa=False, sso_method="pwd")
        assert ans.status == 403
        ok = signin.finish(db, user, facts, methods=["sso:idp_corp"], mfa=True)
        assert ok.status == 200, ok


def test_invitation_acceptance_into_an_sso_domain_gets_no_password_session(world, engine):
    mark_claimed(engine)
    admin = _enrolled_admin(engine, "adm4@example.test")
    _enforce_sso(engine, "corp.test")
    inv = admin.post("/v1/identity/users", json={"email": "new@corp.test"}, headers=csrf(admin))
    assert inv.status_code == 201 and "link" in inv.json(), inv.text
    token = unquote(inv.json()["link"].split("#t=", 1)[1])
    r = fresh_client().post("/v1/auth/invitations/accept",
                            json={"token": token, "name": "New", "password": NEW_PASSWORD}, headers=HEADERS)
    assert r.status_code == 403 and r.json()["error"] == "sso_required", r.text


def test_only_finish_issues_a_new_session():
    """Static: `sessions.issue(` is called only by `signin.finish` and by the three paths that
    ROTATE or bridge an existing credential (each listed with why). A new sign-in path must go
    through `finish`, which is where Enforce SSO is checked."""
    allowed = {
        ("engine/boss_engine/identity/signin.py", "finish"),               # every sign-in
        ("engine/boss_engine/identity/signin.py", "step_up_finish"),       # rotates a live session
        ("engine/boss_engine/identity/signin.py", "shared_password_login"),  # legacy principal, no email
        ("apps/api/app/routers/identity_me.py", "_rotate_fresh"),          # rotates a live session
    }
    found = set()
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in (ROOT / base).rglob("*.py"):
            src = path.read_text()
            if "sessions.issue(" not in src:
                continue
            tree = ast.parse(src)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "issue" and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "sessions"):
                        found.add((str(path.relative_to(ROOT)), fn.name))
    assert found <= allowed, found - allowed
    assert ("engine/boss_engine/identity/signin.py", "finish") in found


# ---- the Owner break-glass still works, and A-2 ----------------------------------------------

def test_host_recovery_is_the_owner_break_glass_under_enforce_sso(world, engine):
    mark_claimed(engine)
    make_user(engine, "boss@corp.test", role="owner")
    _enforce_sso(engine, "corp.test")
    with engine.begin() as db:
        rec = cli.recover_owner("boss@corp.test", db=db, origin="https://boss.example.test")
    _c, r = _reset(unquote(rec.link.split("#t=", 1)[1]))
    assert r.status_code == 200 and r.json()["next"] == "enrol", r.text


def test_recover_owner_assumes_compromise_and_says_so(world, engine):
    mark_claimed(engine)
    owner = make_user(engine, "boss2@example.test", role="owner")
    c = fresh_client()
    sign_in(c, "boss2@example.test")
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_webauthn_credentials (id, principal_id, credential_id, public_key, "
                        "name) VALUES ('pky_intruder', :p, '\\x01'::bytea, '\\x02'::bytea, 'intruder key')"),
                   {"p": owner})
        db.execute(text("INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
                        "created_by) VALUES ('key_intruder', :p, 'pat', 'intruder pat', 'deadbeef', "
                        "ARRAY['project:read'], :p)"), {"p": owner})
    with engine.begin() as db:
        rec = cli.recover_owner("boss2@example.test", db=db, origin="https://boss.example.test")
    with engine.begin() as db:
        pk = db.execute(text("SELECT count(*) FROM identity_webauthn_credentials WHERE principal_id = :p"),
                        {"p": owner}).scalar()
        live_keys = db.execute(text("SELECT count(*) FROM identity_api_credentials WHERE principal_id = :p "
                                    "AND revoked_at IS NULL"), {"p": owner}).scalar()
        live_sessions = db.execute(text("SELECT count(*) FROM identity_sessions WHERE principal_id = :p "
                                        "AND revoked_at IS NULL"), {"p": owner}).scalar()
        details = db.execute(text("SELECT details FROM identity_security_events WHERE event_type = "
                                  "'owner.recovered_by_host' AND target_id = :p"), {"p": owner}).scalar()
    assert (pk, live_keys, live_sessions) == (0, 0, 0)
    assert (rec.passkeys_removed, rec.api_keys_revoked) == (1, 1) and rec.sessions_revoked >= 1
    assert details["assumes_compromise"] is True
    assert details["passkeys_removed"] == 1 and details["personal_keys_revoked"] == 1
    assert details["sessions_revoked"] == rec.sessions_revoked
    out = cli.recovery_summary(rec)
    assert "compromised" in out and "1 passkey(s)" in out and "1 personal API key(s) were revoked" in out
    # and the recovery link now lands in enrolment (no dead end at a lost passkey)
    _c, r = _reset(unquote(rec.link.split("#t=", 1)[1]))
    assert r.status_code == 200 and r.json()["next"] == "enrol", r.text
