"""Notices a person (and their admins) get about their account (FX-B; IDENTITY.md §4.3, §4.7).

  * A sign-in from a device BOSS has not seen for the person emails them (with SMTP), on every
    sign-in method: there is ONE place a session is issued (signin.finish), and the test pins that.
    Without SMTP, /v1/identity/me shows it as a `new_device` notice. A first-ever sign-in, and a
    familiar device, are not news.
  * An admin's MFA reset tells the person and every other Owner and Admin.
"""
from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.identity import authn

ROOT = Path(__file__).resolve().parents[3]


def _subjects(world, to):
    return [s for t, s, _b in world.mail if t == to]


def test_a_sign_in_from_a_new_device_is_emailed_but_not_the_first_or_a_familiar_one(world, engine):
    world.smtp = True
    make_user(engine, "nd@example.test")
    first = fresh_client()
    assert sign_in(first, "nd@example.test").status_code == 200
    assert "New sign-in to BOSS" not in _subjects(world, "nd@example.test")       # first-ever sign-in
    other = fresh_client()
    assert sign_in(other, "nd@example.test").status_code == 200
    assert _subjects(world, "nd@example.test").count("New sign-in to BOSS") == 1
    body = [b for t, s, b in world.mail if s == "New sign-in to BOSS"][0]
    assert "password" in body and "Sessions" in body
    again = sign_in(first, "nd@example.test")                                     # its device cookie
    assert again.status_code == 200
    assert _subjects(world, "nd@example.test").count("New sign-in to BOSS") == 1


#: Sign-ins that are not a PERSON's: the shared console password (fx-a) signs in the legacy bridge
#: principal, which has no devices of its own to be told about; it ends at the legacy cutoff.
BRIDGE_SIGN_INS = frozenset({"engine/boss_engine/identity/signin.py:shared_password_login"})


def test_every_sign_in_method_issues_its_session_in_one_place():
    """The class: the new-device notice lives in signin.finish, so every method that signs a person
    IN must issue its session there. The only other callers of sessions.issue ROTATE a session the
    person already holds (step-up), which they must say with `replaces=` — a sign-in elsewhere
    would skip the notice."""
    callers, rotations_without_replaces = [], []
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in (ROOT / base).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "sessions.issue(" not in source:
                continue
            for fn in ast.walk(ast.parse(source)):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and ast.unparse(node.func) == "sessions.issue":
                        where = f"{path.relative_to(ROOT)}:{fn.name}"
                        callers.append(where)
                        replaces = next((k for k in node.keywords if k.arg == "replaces"), None)
                        if where.endswith(":finish") or where in BRIDGE_SIGN_INS:
                            continue
                        if replaces is None or ast.unparse(replaces.value) == "None":
                            rotations_without_replaces.append(where)
    assert "engine/boss_engine/identity/signin.py:finish" in callers, callers
    assert not rotations_without_replaces, rotations_without_replaces


def test_without_smtp_the_new_device_is_a_notice_in_the_console(world, engine):
    world.smtp = False
    pid = make_user(engine, "nd2@example.test")
    client = fresh_client()
    assert sign_in(client, "nd2@example.test").status_code == 200
    me = client.get("/v1/identity/me").json()
    assert not [b for b in me["banners"] if b["kind"] == "new_device"]        # this sign-in itself
    with engine.begin() as db:                                                # an earlier one elsewhere
        org = db.execute(text("SELECT org_id FROM identity_principals WHERE id = :p"), {"p": pid}).scalar()
        seq = int(db.execute(text("SELECT coalesce(max(seq), 0) + 1 FROM identity_security_events "
                                  "WHERE org_id = :o"), {"o": org}).scalar())
        db.execute(text("INSERT INTO identity_security_events (org_id, seq, stream, event_type, outcome, "
                        "actor_principal_id, session_ref, ip, prev_hash, hash) VALUES (:o, :s, 'security', "
                        "'auth.new_device', 'info', :p, 'ses_elsewhere', '203.0.113.9', :h, :h)"),
                   {"o": org, "s": seq, "p": pid, "h": b"\0" * 32})
    banners = client.get("/v1/identity/me").json()["banners"]
    notice = [b for b in banners if b["kind"] == "new_device"]
    assert notice and "203.0.113.9" in notice[0]["message"]
    assert world.mail == []


def test_a_notice_never_names_a_private_or_container_address(world, engine):
    """Behind the console's proxy the API sees the container's address (172.18.0.8), not the
    person's: a private address is "a new device", never an address that names our network."""
    world.smtp = False
    pid = make_user(engine, "nd3@example.test")
    client = fresh_client()
    assert sign_in(client, "nd3@example.test").status_code == 200
    with engine.begin() as db:
        org = db.execute(text("SELECT org_id FROM identity_principals WHERE id = :p"), {"p": pid}).scalar()
        seq = int(db.execute(text("SELECT coalesce(max(seq), 0) + 1 FROM identity_security_events "
                                  "WHERE org_id = :o"), {"o": org}).scalar())
        db.execute(text("INSERT INTO identity_security_events (org_id, seq, stream, event_type, outcome, "
                        "actor_principal_id, session_ref, ip, prev_hash, hash) VALUES (:o, :s, 'security', "
                        "'auth.new_device', 'info', :p, 'ses_elsewhere', '172.18.0.8', :h, :h)"),
                   {"o": org, "s": seq, "p": pid, "h": b"\0" * 32})
    notice = [b for b in client.get("/v1/identity/me").json()["banners"] if b["kind"] == "new_device"]
    assert notice and notice[0]["message"].startswith("New sign-in from a new device on ")
    assert "172.18" not in notice[0]["message"]


def _admin(engine, email: str, role: str):
    make_user(engine, email, role=role)
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def test_an_mfa_reset_tells_the_person_and_every_other_admin(world, engine):
    world.smtp = True
    owner = _admin(engine, "rs-owner@example.test", "owner")
    make_user(engine, "rs-admin@example.test", role="admin")
    target = make_user(engine, "rs-target@example.test", role="operator")
    world.mail.clear()
    res = owner.post(f"/v1/identity/users/{target}/reset-mfa", headers=csrf(owner))
    assert res.status_code == 200, res.text
    assert res.json()["admins_notified"] >= 2                     # the other admin and the owner
    assert "Your BOSS second factor was reset" in _subjects(world, "rs-target@example.test")
    assert "A BOSS second factor was reset" in _subjects(world, "rs-admin@example.test")
    assert world.events_of("notice.mfa_reset")
