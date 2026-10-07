"""`seed-test-accounts` (identity/seed_accounts.py): Owner-run, refused on a production-looking
install unless --i-know, idempotent, and its secrets go ONLY to the 0600 --out file."""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess

import pytest
from sqlalchemy import text

from boss_engine.identity import passwords, seed_accounts, totp, vault
from boss_engine.identity.seed_accounts import SeedRefused, seed_test_accounts
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    NOW,
    REPO,
    community_install,
    db,
    fake_claim,
    s5_engine,
    settings,
)


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"s" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("BOSS_ENVIRONMENT", "test")
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


def _owner(db):
    community_install(db)
    db.execute(text("DELETE FROM identity_vault_items"))
    db.execute(text("DELETE FROM identity_vault_keks"))
    vault.check_master_key(db=db)
    return fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW, cfg=settings())


def test_it_seeds_one_person_per_role_a_custom_role_and_a_service_account_secrets_only_in_the_file(
        db, tmp_path, capsys, caplog):
    owner = _owner(db)
    out = tmp_path / "seed.json"
    done = seed_test_accounts(db, org_id="org_boss", owner_email="param@example.com", out=str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600 and done.people == 6
    doc = json.loads(out.read_text())
    roles = {p["role"]: p for p in doc["people"]}
    assert set(roles) == {"owner", "admin", "operator", "viewer", "auditor", "developer"}
    for role, person in roles.items():
        phc = db.execute(text("SELECT password_hash FROM identity_users WHERE email = :e"),
                         {"e": person["email"]}).scalar()
        assert passwords.verify_password(person["password"], phc)
        bound = db.execute(text("SELECT r.key FROM identity_role_bindings b JOIN identity_access_roles r "
                                "ON r.id = b.role_id WHERE b.subject_id = :p"), {"p": person["principal_id"]}).scalars().all()
        assert role in bound
        assert ("totp_secret" in person) == (role in ("owner", "admin", "auditor"))
        if "totp_secret" in person:
            assert totp.is_enrolled(db, person["principal_id"])
    custom = db.execute(text("SELECT id FROM identity_access_roles WHERE key = 'developer' AND org_id = 'org_boss'")).scalar()
    assert custom and "approve" not in " ".join(doc["custom_role"]["permissions"])
    assert doc["service_account"]["key"].startswith("boss_sak_")
    events = db.execute(text("SELECT actor_principal_id FROM identity_security_events WHERE details->>'via' = "
                             "'seed_test_accounts'")).scalars().all()
    assert events and set(events) == {owner.principal_id}          # Owner-run, audited as them
    said = capsys.readouterr()
    shown = said.out + said.err + caplog.text
    for person in doc["people"]:
        assert person["password"] not in shown and person.get("totp_secret", "@@") not in shown
    assert doc["service_account"]["key"] not in shown


def test_a_second_run_finds_the_same_accounts_and_gives_them_new_credentials(db, tmp_path):
    _owner(db)
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    seed_test_accounts(db, org_id="org_boss", owner_email="param@example.com", out=str(first))
    seed_test_accounts(db, org_id="org_boss", owner_email="param@example.com", out=str(second))
    a, b = json.loads(first.read_text()), json.loads(second.read_text())
    assert [p["principal_id"] for p in a["people"]] == [p["principal_id"] for p in b["people"]]
    assert all(x["password"] != y["password"] for x, y in zip(a["people"], b["people"]))
    assert a["service_account"]["principal_id"] == b["service_account"]["principal_id"]
    live = db.execute(text("SELECT count(*) FROM identity_api_credentials WHERE principal_id = :p AND "
                           "revoked_at IS NULL"), {"p": b["service_account"]["principal_id"]}).scalar()
    assert live == 1
    assert db.execute(text("SELECT count(*) FROM identity_users WHERE email LIKE 'seed-%'")).scalar() == 6


def test_it_refuses_a_non_owner_and_a_production_looking_install(db, tmp_path, monkeypatch):
    _owner(db)
    out = tmp_path / "x.json"
    with pytest.raises(SeedRefused, match="active Owner"):
        seed_test_accounts(db, org_id="org_boss", owner_email="nobody@example.com", out=str(out))
    monkeypatch.delenv("BOSS_ENVIRONMENT")
    monkeypatch.setattr(seed_accounts, "looks_like_production", lambda db, org: True)
    with pytest.raises(SeedRefused, match="production"):
        seed_test_accounts(db, org_id="org_boss", owner_email="param@example.com", out=str(out))
    assert not out.exists()
    seed_test_accounts(db, org_id="org_boss", owner_email="param@example.com", out=str(out), i_know=True)
    assert out.exists()


def test_production_is_a_public_origin_without_a_test_environment(db, monkeypatch):
    from boss_engine.identity import seed_accounts as sa

    community_install(db)
    monkeypatch.delenv("BOSS_ENVIRONMENT", raising=False)
    for origin, prod in (("https://boss.acme.com", True), ("http://localhost:3000", False),
                         ("https://boss.acme.test", False)):
        monkeypatch.setattr("boss_engine.identity.authn_support.public_origin", lambda db, org=None, o=origin: o)
        assert sa.looks_like_production(db, "org_boss") is prod, origin
    monkeypatch.setenv("BOSS_ENVIRONMENT", "staging")
    assert sa.looks_like_production(db, "org_boss") is False


def test_the_seed_is_host_shell_only():
    hits = subprocess.run(["git", "grep", "-l", "-E", r"seed_accounts|seed_test_accounts", "--",
                           "apps", "engine/boss_engine", "tools", "api"],
                          cwd=REPO, capture_output=True, text=True).stdout.split()
    assert set(hits) <= {"engine/boss_engine/identity/cli.py", "engine/boss_engine/identity/seed_accounts.py"}, hits
