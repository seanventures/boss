"""P1-15: the optional HIBP breach check runs on every path that sets a password (IDENTITY.md §4.2).

hibp_check() existed with no caller, and the policy's `hibp_check` flag was stored and never read.
Now check_new_password takes the org's policy on every path (a scan enforces it), a breached
password is refused (`password_breached`), an unreachable HIBP accepts and posture warns, and with
the check off nothing leaves the host.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import passwords
from boss_engine.identity.policy import IdentityPolicy

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "violet anchor tundra 4471"


def test_every_path_that_sets_a_password_passes_the_policy():
    calls = []
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in (ROOT / base).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "check_new_password(" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("check_new_password"):
                    calls.append((f"{path.relative_to(ROOT)}:{node.lineno}",
                                  any(k.arg == "policy" for k in node.keywords)))
    assert len(calls) >= 5, calls      # claim, reset, signup, change, invitation
    assert not [where for where, ok in calls if not ok], calls


class _Resp:
    def __init__(self, status, body=b""):
        self.status, self.body = status, body


def _fake_hibp(monkeypatch, *, breached: bool = False, reachable: bool = True):
    import hashlib

    from boss_engine.identity import http

    seen = []

    def fetch(method, url, **kw):
        seen.append(url)
        if not reachable:
            raise http.OutboundRefused("blocked")
        digest = hashlib.sha1(passwords.normalize(PASSWORD).encode()).hexdigest().upper()  # noqa: S324
        line = f"{digest[5:]}:{42 if breached else 0}\r\nAAAA:1"
        return _Resp(200, line.encode())

    monkeypatch.setattr(http, "fetch", fetch)
    return seen


def test_a_breached_password_is_refused_when_the_check_is_on(monkeypatch):
    seen = _fake_hibp(monkeypatch, breached=True)
    with pytest.raises(passwords.PasswordRejected) as refused:
        passwords.check_new_password(PASSWORD, policy=IdentityPolicy(hibp_check=True))
    assert refused.value.code == "password_breached"
    assert seen and seen[0].startswith("https://api.pwnedpasswords.com/range/")
    assert passwords.normalize(PASSWORD) not in seen[0]           # k-anonymity: a 5-char prefix only


def test_unreachable_hibp_accepts_and_is_remembered_for_posture(monkeypatch):
    monkeypatch.setattr(passwords, "_HIBP_UNREACHABLE_AT", None)
    _fake_hibp(monkeypatch, reachable=False)
    assert passwords.check_new_password(PASSWORD, policy=IdentityPolicy(hibp_check=True))
    assert passwords.hibp_unreachable_since()


def test_with_the_check_off_nothing_leaves_the_host(monkeypatch):
    seen = _fake_hibp(monkeypatch, breached=True)
    assert passwords.check_new_password(PASSWORD, policy=IdentityPolicy(hibp_check=False))
    assert seen == []


def test_the_policys_minimum_length_applies_through_the_policy():
    with pytest.raises(passwords.PasswordRejected) as refused:
        passwords.check_new_password("tundra violet 9", policy=IdentityPolicy(password_min_length=20))
    assert refused.value.code == "password_too_short"


def test_changing_your_password_to_a_breached_one_is_refused_over_http(engine, monkeypatch):
    world = IdentityWorld.create(engine)
    person = world.person("pw-changer")
    with engine.begin() as db:
        db.execute(text("INSERT INTO identity_users (principal_id, email) VALUES (:p, 'pw@acme.test')"),
                   {"p": person.id})
        db.execute(text("INSERT INTO identity_settings (org_id, policy, epoch) VALUES (:o, CAST(:p AS jsonb), 1) "
                        "ON CONFLICT (org_id) DO UPDATE SET policy = EXCLUDED.policy, "
                        "epoch = identity_settings.epoch + 1"),
                   {"o": world.org, "p": json.dumps({"hibp_check": True})})
    _fake_hibp(monkeypatch, breached=True)
    client = TestClient(app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(person)):
        out = client.post("/v1/identity/me/password", json={"new": PASSWORD},
                          headers={"sec-fetch-site": "same-origin"})
    assert out.status_code == 422 and out.json().get("error") == "password_breached", out.text
