"""A Codex session rotates, and BOSS has to keep up with it.

These CLIs refresh their credential ON USE, and the refresh REVOKES the previous one. That makes
a stored copy correct for exactly as long as nobody uses it, and it produced two failures on a
real install:

  * the CLI reported "Logged in using ChatGPT" while every turn failed
    `401 … invalidated oauth token`, because a refresh elsewhere had revoked the copy it held;
  * the store kept the credential the operator first sent, so the first `up --build` after a
    rotation restored a token that had been dead for weeks.

Neither is fixable by copying more carefully. What fixes them is noticing the specific failure
and healing from it, and writing the rotated credential back so the store does not decay.

Verified against the real messages codex-cli emits, not invented ones.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.config import Settings
from boss_engine.services.cli_harness import (
    BUILTIN_CLI_HARNESSES,
    GenericCliCodeAgent,
    _looks_like_revoked_credential,
)

CREDENTIAL = {"tokens": {"access_token": "a.b.c", "refresh_token": "r"}}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    return GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])


# --- telling a dead credential from bad work ---------------------------------


@pytest.mark.parametrize("message", [
    # Both captured from codex-cli 0.154.0 on a real install.
    "ERROR: Your access token could not be refreshed because your refresh token was revoked. "
    "Please log out and sign in again.",
    "401 Unauthorized: Encountered invalidated oauth token for user, failing request",
])
def test_a_revoked_credential_is_recognised(message: str) -> None:
    assert _looks_like_revoked_credential(message)


@pytest.mark.parametrize("message", [
    "error: no such file or directory",
    "the tests failed: 3 assertions",
    "rate limit exceeded",
    "",
])
def test_ordinary_failures_are_not_mistaken_for_it(message: str) -> None:
    """Retrying a bad turn spends the operator's subscription to reach the same answer."""
    assert not _looks_like_revoked_credential(message)


# --- keeping the store current ----------------------------------------------


class _Store:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get(self, key):
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]

    def put(self, key, value):
        self.values[key] = value
        self.writes.append(key)


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    monkeypatch.setattr("boss_engine.services.secret_backends.backend_for",
                        lambda kind, settings: s)
    return s


def test_a_rotated_credential_is_written_back(agent, store) -> None:
    """Otherwise the store decays, and the next rebuild restores a revoked token."""
    store.values["BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"] = json.dumps(CREDENTIAL)
    rotated = json.dumps({"tokens": {"access_token": "new.token.here"}})
    agent._write_auth_file(rotated)

    assert agent.remember_rotated_credential() is True
    assert json.loads(store.values["BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"]) == json.loads(rotated)


def test_an_unchanged_credential_is_not_written_back(agent, store) -> None:
    """Churning the store on every run buys nothing and writes a secret for no reason."""
    same = json.dumps(CREDENTIAL)
    store.values["BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"] = same
    agent._write_auth_file(same)

    assert agent.remember_rotated_credential() is False
    assert store.writes == []


def test_an_operator_set_variable_is_never_written_over(agent, store, monkeypatch) -> None:
    """They own that value. Rewriting it would take a decision that is theirs."""
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(CREDENTIAL))
    agent._write_auth_file(json.dumps({"tokens": {"access_token": "rotated"}}))

    assert agent.remember_rotated_credential() is False
    assert store.writes == []


def test_a_write_back_failure_never_breaks_the_run(agent, monkeypatch) -> None:
    """The store is a cache here. A run that produced real work must not fail over one."""
    agent._write_auth_file(json.dumps(CREDENTIAL))
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: (_ for _ in ()).throw(RuntimeError("store is down")))

    assert agent.remember_rotated_credential() is False


def test_nothing_is_written_back_when_there_is_no_credential(agent, store) -> None:
    assert agent.remember_rotated_credential() is False
    assert store.writes == []


# --- never replace a newer credential with an older one ----------------------


def test_a_stale_environment_value_cannot_overwrite_a_newer_session(agent, monkeypatch) -> None:
    """THE failure that took four attempts to find, and it was never rotation.

    A CODEX_AUTH_JSON left in a .env by an experiment three days earlier was forwarded into the
    container, and `_resolve_secret` reads the environment BEFORE the store. So every Connect
    restored that dead credential over a session created minutes before — including one made
    with `codex login --device-auth` — and the CLI then failed 401 on a credential BOSS had put
    there itself. Re-sending never helped: sending updates the store, and the environment won.
    """
    fresh = json.dumps({"tokens": {"access_token": "new"}, "last_refresh": "2026-09-18T01:24:00Z"})
    stale = json.dumps({"tokens": {"access_token": "old"}, "last_refresh": "2026-09-15T21:55:41Z"})
    agent._write_auth_file(fresh)
    monkeypatch.setenv("CODEX_AUTH_JSON", stale)

    result = agent._authorise_from_file(cli_reports_signed_in=True)

    assert result["authorised"] is True
    assert json.loads(agent._current_auth_file())["tokens"]["access_token"] == "new", (
        "a three-day-old credential overwrote a newer one")


def test_a_newer_stored_credential_is_still_applied(agent, monkeypatch) -> None:
    """The guard must not freeze a genuinely stale session in place."""
    old = json.dumps({"tokens": {"access_token": "old"}, "last_refresh": "2026-09-15T21:55:41Z"})
    new = json.dumps({"tokens": {"access_token": "new"}, "last_refresh": "2026-09-18T01:24:00Z"})
    agent._write_auth_file(old)
    monkeypatch.setenv("CODEX_AUTH_JSON", new)
    monkeypatch.setattr(
        "boss_engine.services.cli_harness.detect",
        lambda spec: {"installed": True, "authenticated": True, "auth_detail": "ok"})

    agent._authorise_from_file(cli_reports_signed_in=True)

    assert json.loads(agent._current_auth_file())["tokens"]["access_token"] == "new"


def test_undatable_credentials_fall_back_to_the_old_behaviour() -> None:
    """With no timestamp on either side, guessing which is newer would be worse than not."""
    from boss_engine.services.cli_harness import _newer_credential

    assert _newer_credential('{"tokens":{}}', '{"tokens":{}}') is None
    assert _newer_credential("not json", '{"last_refresh":"2026-09-18T00:00:00Z"}') is None
    assert _newer_credential(None, None) is None


def test_the_timestamp_comparison_is_the_one_the_files_carry() -> None:
    """Measured from real auth.json files: the field is `last_refresh`, ISO-8601, UTC."""
    from boss_engine.services.cli_harness import _credential_issued_at

    assert _credential_issued_at('{"last_refresh": "2026-09-18T00:10:30.629768Z"}') == \
        "2026-09-18T00:10:30.629768Z"
    assert _credential_issued_at('{"tokens": {"access_token": "x"}}') is None
