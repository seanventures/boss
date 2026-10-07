"""The console walk signs in as a PERSON, through the real sign-in page (IDENTITY.md §4).

The incident: the console's shared-password page (app/login/page.tsx, #console-password) was
deleted when identity landed, and the walk kept typing into it — every screen became a 60 s
timeout and the harness reported the console broken when it was the harness. These tests hold
the harness to the sign-in that exists: claim the first Owner, seed one person per role, sign in
with email + password + authenticator code, and never let a credential reach a log line.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from e2e.harness import REPO_ROOT, identity
from e2e.harness import console as console_mod
from e2e.harness import stack as stack_mod
from e2e.harness.target import Target, TargetError, live_target_from_env

HARNESS = REPO_ROOT / "tests" / "e2e" / "harness"
SEED = REPO_ROOT / "engine" / "boss_engine" / "identity" / "seed_accounts.py"


def test_nothing_in_the_harness_types_the_old_console_password():
    for path in HARNESS.glob("*.py"):
        assert "#console-password" not in path.read_text(), f"{path.name} still uses the deleted page"
    assert not (REPO_ROOT / "apps" / "web" / "app" / "login" / "page.tsx").exists() or \
        'id="console-password"' not in (REPO_ROOT / "apps" / "web" / "app" / "login" / "page.tsx").read_text()


def test_every_sign_in_selector_is_in_the_console_source():
    for rel, literal, use in identity.SIGN_IN_SELECTORS:
        source = (REPO_ROOT / "apps" / "web" / rel).read_text()
        assert literal in source, f"apps/web/{rel} no longer has {literal!r} ({use})"
    walked = {literal for _, literal, _ in console_mod.SOURCE_CONTRACT}
    assert {literal for _, literal, _ in identity.SIGN_IN_SELECTORS} <= walked


def test_totp_is_rfc_6238():
    # RFC 6238 Appendix B, SHA-1, T = 59 s: 94287082 → the 6-digit code is its last six digits.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # base32 of "12345678901234567890"
    assert identity.totp(secret, at=59) == "287082"
    assert identity.totp(secret, at=1111111109) == "081804"


def test_the_accounts_file_reader_reads_what_the_seed_writes():
    """load_accounts reads the keys seed_accounts.py writes, by name — a renamed key in the seed
    must break this, not every sign-in of every walk."""
    seed = SEED.read_text()
    for key in ('"people"', '"service_account"', '"role"', '"email"', '"password"', '"principal_id"',
                '"totp_secret"', '"key"', '"scopes"', '"org_id"'):
        assert key in seed, f"seed_accounts.py no longer writes {key}"


def test_an_account_never_shows_its_secrets(tmp_path):
    doc = {"org_id": "org_x", "people": [
        {"role": "owner", "email": "seed-owner@example.test", "password": "pw-SECRET-1",
         "principal_id": "usr_1", "totp_secret": "JBSWY3DPEHPK3PXP"},
        {"role": "viewer", "email": "seed-viewer@example.test", "password": "pw-SECRET-2",
         "principal_id": "usr_2"}],
        "service_account": {"name": "seed-ci", "principal_id": "sva_1", "role": "viewer",
                            "scopes": ["project:read"], "key": "boss_sak_SECRET"}}
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(doc))
    accounts = identity.load_accounts(path)
    shown = repr(accounts) + str(accounts["owner"]) + str(accounts["service_account"])
    assert "SECRET" not in shown and "JBSWY3DP" not in shown
    assert accounts["owner"].totp_secret and accounts["viewer"].totp_secret is None
    assert accounts["service_account"].scopes == ("project:read",)


def test_the_walk_refuses_to_start_without_people(tmp_path, capsys):
    target = Target(kind="live", api_url="http://127.0.0.1:9", owner_key="k", worker_key=None,
                    console_url="http://127.0.0.1:9")
    assert console_mod.walk(target, tmp_path) == 2
    assert "BOSS_E2E_ACCOUNTS_FILE" in capsys.readouterr().err


def test_the_walk_names_a_missing_role(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"org_id": "o", "people": []}))
    target = Target(kind="live", api_url="http://x", owner_key="k", worker_key=None,
                    console_url="http://x", accounts_file=str(path), walk_role="auditor")
    walk = console_mod.Walk(target, tmp_path, allow_writes=False, headed=False)
    with pytest.raises(TargetError, match="auditor"):
        walk.sign_in(page=None, rec=None)


def test_a_live_target_reads_its_people_from_the_environment():
    target = live_target_from_env({"BOSS_E2E_API_URL": "http://h", "BOSS_E2E_OWNER_KEY": "k",
                                   "BOSS_E2E_ACCOUNTS_FILE": "/tmp/a.json", "BOSS_E2E_ROLE": "viewer"})
    assert target.accounts_file == "/tmp/a.json" and target.walk_role == "viewer"
    assert live_target_from_env({"BOSS_E2E_API_URL": "http://h", "BOSS_E2E_OWNER_KEY": "k"}).walk_role == "owner"


def test_a_stack_with_a_console_claims_and_seeds_before_anyone_walks(tmp_path, monkeypatch):
    calls: list[str] = []
    for name in ("start_postgres", "start_stubs", "start_api", "seed", "start_worker", "start_web",
                 "claim_and_seed_people"):
        monkeypatch.setattr(stack_mod.LocalStack, name, lambda self, n=name: calls.append(n))
    monkeypatch.setattr(stack_mod.StackState, "save", lambda self: tmp_path)
    stack = stack_mod.LocalStack(tmp_path / "run", with_web=True)
    stack.up()
    assert calls.index("claim_and_seed_people") == calls.index("start_web") + 1
    calls.clear()
    stack_mod.LocalStack(tmp_path / "run2", with_web=False).up()
    assert "claim_and_seed_people" not in calls


def test_the_claim_goes_through_the_console_origin(monkeypatch):
    """The claim binds the public origin to its Origin header (H2): it must be the console's."""
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["url"], seen["origin"] = req.full_url, req.headers.get("Origin")
        seen["body"] = json.loads(req.data.decode())
        return Resp()

    monkeypatch.setattr(identity.urllib.request, "urlopen", fake_urlopen)
    identity.claim_owner("http://127.0.0.1:5555/", "bst_tok", identity.Claimant(password="p" * 20))
    assert seen["url"] == "http://127.0.0.1:5555/v1/auth/setup/claim"
    assert seen["origin"] == "http://127.0.0.1:5555"
    assert seen["body"]["setup_token"] == "bst_tok" and "legacy_password" not in seen["body"]


def test_the_seeded_file_is_private(tmp_path):
    out = tmp_path / "seed.json"

    def host(argv):
        assert argv[:3] == ["-m", "boss_engine.identity.cli", "seed-test-accounts"]
        Path(argv[argv.index("--out") + 1]).write_text("{}")
        os.chmod(argv[argv.index("--out") + 1], 0o644)

        class Done:
            returncode, stdout, stderr = 0, "", ""
        return Done()

    identity.seed_accounts(host, "e2e-owner@example.test", out)
    assert (out.stat().st_mode & 0o777) == 0o600


def test_a_failed_setup_link_does_not_echo_the_link():
    class Done:
        returncode = 1
        stdout = "Claim this BOSS within 1 hour:\n  http://h/claim#t=bst_SECRETTOKEN\n"
        stderr = "nope"
    with pytest.raises(identity.IdentitySetupError) as err:
        identity.setup_token(lambda argv: Done())
    assert "SECRETTOKEN" not in str(err.value)
    assert re.search(r"#t=", str(err.value)) is None
