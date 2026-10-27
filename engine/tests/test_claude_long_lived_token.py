"""The Claude subscription runs on a LONG-LIVED token BOSS obtains itself (2026-10-27).

The console's Sign in ran `claude auth login`: the CLI's ~8 h login with a rotating refresh
token, wiped twice when a refresh failed. It now runs `claude setup-token` on the BOSS server
(subscription_sign_in) and stores the 1-year token it prints as the subscription credential.
These checks: the token is stored with its origin and no part of its value, it resolves as kind
subscription ahead of the CLI login and reaches a sandboxed child's environment, Connect never
retires it (only the retired paste import is "legacy"), and the CLI login is still the fallback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import claude_credential as cc
from boss_engine.services import cli_harness

#: Not a real credential.
TOKEN = "sk-ant-oat01-" + "FakeT0ken_-x" * 8
SPEC = cli_harness.BUILTIN_CLI_HARNESSES["claude_code"]


@pytest.fixture()
def store(monkeypatch):
    """BOSS's secret store, by name (what a process with no database reads)."""
    held: dict[str, str] = {}
    monkeypatch.delenv(cc.TOKEN_NAME, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli_harness, "stored_credential",
                        lambda _s, name, **_k: held.get(name))
    monkeypatch.setattr(cc, "_store_lookup", lambda _s, name: held.get(name, ""))
    return held


def _boss_token(held: dict[str, str], token: str = TOKEN) -> None:
    held[cc.TOKEN_NAME] = token
    held[cc.ORIGIN_NAME] = cc._origin_marker(token)


def _cli_login(tmp_path: Path, monkeypatch) -> None:
    conf = tmp_path / "service-claude"
    conf.mkdir(mode=0o700)
    path = conf / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "ACCESS_MARKER"}}))
    os.chmod(path, 0o600)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(conf))


# ── origin: BOSS's own token is never legacy ────────────────────────────────────────────────

def test_a_token_boss_obtained_is_not_legacy_and_an_imported_one_is(store):
    _boss_token(store)
    assert cc.boss_obtained_token_stored(Settings()) is True
    assert cli_harness.legacy_token_stored(SPEC, Settings()) is False
    # The retired paste import: a token with no marker, or a marker for another token.
    store.pop(cc.ORIGIN_NAME)
    assert cli_harness.legacy_token_stored(SPEC, Settings()) is True
    store[cc.ORIGIN_NAME] = cc._origin_marker("sk-ant-oat01-someother" + "y" * 40)
    assert cc.boss_obtained_token_stored(Settings()) is False
    assert cli_harness.legacy_token_stored(SPEC, Settings()) is True


def test_the_marker_holds_a_hash_never_the_token():
    marker = cc._origin_marker(TOKEN)
    assert marker.startswith(cc.ORIGIN_BOSS_SETUP_TOKEN + ":")
    assert TOKEN not in marker and TOKEN[-8:] not in marker and TOKEN[13:25] not in marker


class _Refuses:
    """A connection that fails the test if anything is executed on it."""

    def execute(self, *_a, **_k):
        raise AssertionError("retire_legacy_token touched BOSS's own token")


def test_connect_never_retires_a_token_boss_obtained(store):
    _boss_token(store)
    assert cc.retire_legacy_token(_Refuses(), Settings(), actor="operator") == 0
    assert store[cc.TOKEN_NAME] == TOKEN


def test_connect_with_boss_token_and_cli_login_does_not_retire_it(store, monkeypatch):
    """The 83790f618 branch (CLI signed in + a stored token → retire) is for the import only."""
    from boss_engine.services import core, setup_service

    _boss_token(store)
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(cli_harness, "_run_probe", lambda _b, args, **_k: (
        (0, "2.1.197 (Claude Code)") if args == SPEC.version_args else
        (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))))
    retired = []
    monkeypatch.setattr(cc, "retire_legacy_token",
                        lambda *_a, **_k: retired.append(1) or 0)
    monkeypatch.setattr(core, "emit_event", lambda *a, **_k: "evt")

    class Proved(Exception):
        pass

    def prove():
        raise Proved()

    with pytest.raises((Proved, setup_service.SetupError)) as caught:
        setup_service.connect_coding_agent(None, Settings(), "p", "claude_code",
                                           actor="operator", prove_with=prove)
    assert retired == []
    assert "legacy" not in str(caught.value).lower()
    assert store[cc.TOKEN_NAME] == TOKEN


# ── resolution: the token wins, reaches every child, the CLI login is the fallback ───────────

def test_the_long_lived_token_wins_over_the_cli_login(store, tmp_path, monkeypatch):
    _cli_login(tmp_path, monkeypatch)
    _boss_token(store)
    cred = cc.resolve_claude_credential(Settings())
    assert cred.kind == cc.KIND_SUBSCRIPTION and cred.billing == "subscription"
    assert TOKEN not in repr(cred)
    env = cred.apply({"ANTHROPIC_API_KEY": "sk-ant-inherited", "PATH": "/bin"})
    assert env[cc.TOKEN_NAME] == TOKEN and "ANTHROPIC_API_KEY" not in env
    # A child with no connection (bound settings) keeps the answer.
    assert cc.resolve_claude_credential(cred.bind(Settings())).kind == cc.KIND_SUBSCRIPTION


def test_the_token_reaches_a_sandboxed_child(store, tmp_path, monkeypatch):
    from boss_engine.services.sandbox_identity import SandboxIdentity

    _boss_token(store)
    env = cc.resolve_claude_credential(Settings()).apply({"PATH": "/bin"})
    ident = SandboxIdentity(key="t", uid=61000, run_dir=tmp_path / "run")
    child = ident.env(env)
    assert child[cc.TOKEN_NAME] == TOKEN
    assert child["CLAUDE_CONFIG_DIR"] == f"{ident.home}/.claude"


def test_a_sandbox_on_the_token_gets_no_login_copy(store, tmp_path, monkeypatch):
    from dataclasses import dataclass

    from boss_engine.services import sandbox_identity

    @dataclass
    class _Ident:
        home: Path
        key: str = "t"

    _cli_login(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox_identity, "get_settings", Settings, raising=False)
    home = tmp_path / "run-home"
    home.mkdir()
    _boss_token(store)
    assert sandbox_identity.runs_on_long_lived_token() is True
    sandbox_identity._seed_claude_login(_Ident(home))
    assert not (home / ".claude").exists()


def test_without_the_token_the_cli_login_is_the_fallback(store, tmp_path, monkeypatch):
    from dataclasses import dataclass

    from boss_engine.services import claude_login, sandbox_identity

    @dataclass
    class _Ident:
        home: Path
        key: str = "t"

    _cli_login(tmp_path, monkeypatch)
    cred = cc.resolve_claude_credential(Settings())
    assert cred.kind == cc.KIND_CLI_LOGIN
    assert sandbox_identity.runs_on_long_lived_token() is False
    monkeypatch.setattr(claude_login, "ensure_fresh", lambda _n: (True, ""))
    home = tmp_path / "run-home"
    home.mkdir()
    sandbox_identity._seed_claude_login(_Ident(home))
    assert (home / ".claude" / ".credentials.json").exists()


# ── storing it: origin and dates recorded, no part of the value ─────────────────────────────

class _Conn:
    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), dict(params or {})))


def test_store_records_origin_and_renewal_never_the_value(monkeypatch):
    from boss_engine.services import core, secret_backends, setup_service

    put: dict[str, str] = {}

    class _Backend:
        def put(self, key, value):
            put[key] = value

    stored_with = {}

    def store_credential(_c, _s, project_id, provider, credential, *, actor):
        stored_with.update(project=project_id, provider=provider, value=credential, actor=actor)
        return {"backend": "database", "ref_key": "BOSS_SUBSCRIPTION_CLAUDE_CODE_OAUTH_TOKEN"}

    events = []
    monkeypatch.setattr(setup_service, "store_coding_agent_credential", store_credential)
    monkeypatch.setattr(secret_backends, "backend_for", lambda _k, _s: _Backend())
    monkeypatch.setattr(core, "emit_event", lambda *a, **_k: events.append(a) or "evt")
    monkeypatch.setattr(cc, "boss_obtained_token_stored", lambda _s: False)
    monkeypatch.setattr(cc, "retire_legacy_token", lambda *_a, **_k: 1)
    conn = _Conn()
    out = cc.store_long_lived_token(conn, Settings(), "home", TOKEN, actor="operator",
                                    valid_days=365)
    assert stored_with == {"project": "home", "provider": "claude_code", "value": TOKEN,
                           "actor": "operator"}
    assert put == {"BOSS_SUBSCRIPTION_CLAUDE_CODE_OAUTH_TOKEN_ORIGIN": cc._origin_marker(TOKEN)}
    meta = json.loads(conn.statements[-1][1]["m"])
    assert meta["origin"] == cc.ORIGIN_BOSS_SETUP_TOKEN and meta["last4"] is None
    assert meta["expires_at"] > meta["obtained_at"]
    said = json.dumps([out, conn.statements, [e[1:] for e in events]], default=str)
    assert TOKEN not in said and TOKEN[-6:] not in said
    assert events[0][2] == "entitlement.subscription_token_obtained"


def test_a_database_store_refusal_is_shown_in_boss_words(monkeypatch):
    from boss_engine.services import setup_service
    from boss_engine.services.subscription_sign_in import StoreRefused

    def refuse(*_a, **_k):
        raise setup_service.SetupError("Re-run the installer — it writes the key.")

    monkeypatch.setattr(cc, "store_long_lived_token", refuse)

    class _Engine:
        def begin(self):
            import contextlib
            return contextlib.nullcontext(object())

    with pytest.raises(StoreRefused, match="Re-run the installer"):
        cc.sign_in_store(_Engine(), Settings(), "p", "operator")(TOKEN, valid_days=365)


# ── what Repo and models says ───────────────────────────────────────────────────────────────

def test_the_row_says_which_credential_runs(monkeypatch):
    monkeypatch.setattr(cc, "long_lived_token_info", lambda _c, _p: {
        "origin": cc.ORIGIN_BOSS_SETUP_TOKEN, "obtained_at": "2026-10-27T10:00:00+00:00",
        "expires_at": "2027-10-27T10:00:00+00:00"})
    summary = cc.credential_summary(object(), "p")
    assert summary["kind"] == "long_lived_token"
    assert summary["label"] == "long-lived subscription token (renews Oct 27, 2027)"
    monkeypatch.setattr(cc, "long_lived_token_info", lambda _c, _p: None)
    login = cc.credential_summary(object(), "p", login={"state": "ok", "seconds_left": 7 * 3600 + 5})
    assert login == {"kind": "cli_login", "label": "CLI login (expires in 7 h)", "expires_at": None}
    assert cc.credential_summary(object(), "p", login={"state": "absent"}) is None


def test_the_cli_login_expiry_never_reddens_a_token_row():
    from boss_engine.services import setup_view

    expired = {"state": "expired", "seconds_left": 0}
    assert setup_view.claude_login_state("working", provider="claude_code", kind="subscription",
                                         login=expired)[0] == "login_expired"
    assert setup_view.claude_login_state("working", provider="claude_code", kind="subscription",
                                         login=expired, long_lived=True)[0] == "working"
