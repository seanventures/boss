"""Integration wiring the five identity streams could not pin on their own (lead, id/integration)."""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.identity import change_feed
from boss_engine.identity.enforcement import Outcome, evaluate
from boss_engine.identity.declarations import Permit
from boss_engine.identity.scope import Scope

ROOT = Path(__file__).resolve().parents[2]


def test_the_epoch_triggers_live_in_the_schema_and_match_what_the_feed_watches():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert f"CREATE OR REPLACE FUNCTION {change_feed.FUNCTION}()" in schema
    for table, columns in change_feed.WATCHED.items():
        name = f"identity_epoch_{table.removeprefix('identity_')}"
        update = "UPDATE" + (f" OF {', '.join(columns)}" if columns else "")
        pattern = (rf"CREATE TRIGGER {name} AFTER INSERT OR DELETE OR {re.escape(update)}\s+"
                   rf"ON {table} FOR EACH STATEMENT EXECUTE FUNCTION {change_feed.FUNCTION}\(\)")
        assert re.search(pattern, schema), f"{name} is missing or differs from WATCHED"


class _Refused(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code, self.status = code, status


def _outcome(code: str, status: int) -> Outcome:
    def authenticate():
        raise _Refused(code, status)

    return evaluate(Permit("settings:read", Scope.org()), authenticate=authenticate,
                    values=lambda: None, org_id=lambda ctx: "org", check=None, resolve=None)


def test_s1_1_a_csrf_failure_is_403_and_a_cookie_plus_bearer_is_400_not_401():
    csrf = _outcome("csrf_failed", 403)
    assert (csrf.status, csrf.error_body()) == (403, {"error": "csrf_failed"})
    both = _outcome("ambiguous_credentials", 400)
    assert (both.status, both.error_body()) == (400, {"error": "ambiguous_credentials"})
    # a plain 401 keeps the spec body and never names why (no account oracle)
    bad = _outcome("invalid_credentials", 401)
    assert (bad.status, bad.error_body()) == (401, {"error": "unauthenticated"})


def test_a_compose_install_gets_a_persistent_master_key_file_and_server_key(tmp_path, monkeypatch):
    """No installer step: at FIRST INSTALL the vault's boot step creates the key file once (0600),
    every process reads the same one, and the server key (CSRF, hashed identifiers) derived from it
    survives a restart. A runtime read never creates it (#10)."""
    import os
    import stat

    import pytest as _pytest

    from boss_engine.config import get_settings
    from boss_engine.identity import authn_support, vault

    key_file = tmp_path / "identity" / "master.key"
    monkeypatch.delenv("BOSS_MASTER_KEY", raising=False)
    monkeypatch.setenv("BOSS_MASTER_KEY_FILE", str(key_file))
    monkeypatch.setattr(get_settings(), "identity_server_key", "")
    vault.reset_master_key_cache()
    with _pytest.raises(vault.MasterKeyMissing):
        vault._read_master_key()                  # a runtime read: no key appears
    assert not key_file.exists()
    vault._provision_key_file(key_file)           # what boot() does at first install
    authn_support.reset_server_key_for_tests()
    first = authn_support.server_key()
    assert key_file.exists() and stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    written = key_file.read_bytes()
    authn_support.reset_server_key_for_tests()   # "a restart"
    vault.reset_master_key_cache()
    assert authn_support.server_key() == first
    assert key_file.read_bytes() == written       # never rewritten once it exists
    vault._provision_key_file(key_file)           # a second process racing the first: no-op
    assert key_file.read_bytes() == written
    authn_support.reset_server_key_for_tests()
    vault.reset_master_key_cache()


def test_first_boot_provisions_the_vault_key_before_anything_needs_the_server_key():
    """Integration round 2 (a): server_key() fails closed (F3) and the key file is made only by
    vault.boot at first install (F1). The claim link mints a setup token (keyed hash), so the
    lifespan must boot the vault first — otherwise a fresh compose install printed no claim link."""
    main = (ROOT / "apps" / "api" / "app" / "main.py").read_text(encoding="utf-8")
    # The link is announced by the lifespan's keeper task (announce_claim_link_once, at boot and
    # every CLAIM_LINK_REANNOUNCE_SECONDS); the task starts only after the vault booted.
    boot = main.index("_identity_vault.boot(db=_connection)")
    claim = main.index("_asyncio.create_task(_keep_claim_link_announced())")
    assert boot < claim
    assert "_identity_claim.announce_setup_link(connection)" in main
    compose = (ROOT / "scripts" / "install" / "community-compose.yml").read_text(encoding="utf-8")
    for service in ("api", "worker", "egress"):
        assert compose.count("BOSS_MASTER_KEY_FILE: ${BOSS_MASTER_KEY_FILE:-/var/lib/boss/identity/master.key}") >= 3
