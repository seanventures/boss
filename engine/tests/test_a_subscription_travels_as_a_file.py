"""Codex reaches a server by its credential FILE, because the token route does not exist.

This was documented and shipped the other way round first, on the reasonable assumption that a
flag named `--with-access-token` accepts an access token. Measured against codex-cli 0.154.0,
with a real ChatGPT subscription, every field its auth file holds:

    access_token   -> Error logging in with access token: agent identity JWT payload is not
                      valid JSON
    id_token       -> same
    refresh_token  -> agent identity JWT payload is not valid base64url

The token is not malformed — decoded, it is a valid JWT carrying proper OAuth claims. The flag
wants a different credential type. And the failure is not graceful: merely having
CODEX_ACCESS_TOKEN in the environment makes `codex login status` ERROR instead of reporting a
status, so detection breaks rather than degrades.

Restoring the file itself produces "Logged in using ChatGPT".

So these tests pin the shape of the route that works, and — as much as anything can — that
nobody quietly reintroduces the one that does not.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from boss_engine.config import Settings
from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES, GenericCliCodeAgent

CREDENTIAL = {"tokens": {"access_token": "a.b.c", "refresh_token": "rt"}, "last_refresh": "x"}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])


def test_codex_declares_the_file_route_and_not_the_token_one() -> None:
    spec = BUILTIN_CLI_HARNESSES["codex"]

    assert spec.auth_file_secret == "CODEX_AUTH_JSON"
    assert spec.auth_file_name == "auth.json"
    assert spec.home_env == "CODEX_HOME"
    # The token route is not merely unused, it is actively harmful: CODEX_ACCESS_TOKEN in the
    # environment makes `codex login status` error rather than answer.
    assert not spec.token_env
    assert not spec.token_login_args


def test_claude_code_still_travels_as_a_token() -> None:
    """The CLI that DOES mint a headless token must not be dragged onto the file route.

    claude_code is not a CLI spec — it predates them and has its own agent — so the thing to
    check is that its setting still exists and that no spec has claimed the variable.
    """
    assert "claude_code_oauth_token" in Settings.model_fields
    assert not any(spec.auth_file_secret == "CLAUDE_CODE_OAUTH_TOKEN"
                   for spec in BUILTIN_CLI_HARNESSES.values())


def test_only_a_spec_that_was_measured_claims_a_file_route() -> None:
    """A file route is a claim about a specific CLI's behaviour, verified by running it.

    Copying it onto cursor or aider because it looks similar is how a registry stops meaning
    anything — the same reason none of these ship `verified: True`.
    """
    with_file_route = sorted(n for n, s in BUILTIN_CLI_HARNESSES.items() if s.auth_file_secret)

    assert with_file_route == ["codex"]


def test_the_credential_file_is_never_world_readable(agent, tmp_path) -> None:
    """Created 0600 before anything is written, not chmod-ed afterwards.

    Opening 0644 and fixing the mode later leaves a window in which any process on the box can
    read a live session — and on a box running a coding agent, "any process" includes work
    BOSS itself scheduled.
    """
    written = agent._write_auth_file(json.dumps(CREDENTIAL))

    assert json.loads(open(written).read()) == CREDENTIAL
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(written)).st_mode) == 0o700


def test_the_home_env_var_decides_where_it_goes(agent, tmp_path) -> None:
    assert agent.cli_home() == str(tmp_path / "codex-home")
    assert agent._write_auth_file("{}").endswith("codex-home/auth.json")


def test_a_write_leaves_no_temporary_file_behind(agent) -> None:
    """A half-written credential under a predictable name is a credential on disk."""
    agent._write_auth_file(json.dumps(CREDENTIAL))

    leftovers = [f for f in os.listdir(agent.cli_home()) if f.endswith(".boss-tmp")]
    assert not leftovers, leftovers


def test_malformed_stored_json_is_refused_without_quoting_it(agent, monkeypatch) -> None:
    """Writing garbage would leave the CLI signed out and the reason unexplained."""
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"tokens": {"access_token": "super-secret" ')

    result = agent._authorise_from_file()

    assert result["authorised"] is False
    assert "not valid JSON" in result["reason"]
    assert "super-secret" not in result["reason"]


def test_a_missing_credential_names_what_to_set(agent, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    monkeypatch.setattr(type(agent), "_resolve_secret", lambda self, name: None)

    result = agent._authorise_from_file()

    assert result["authorised"] is False
    assert "CODEX_AUTH_JSON" in result["reason"]


def test_writing_the_file_is_not_treated_as_proof_of_a_login(agent, monkeypatch) -> None:
    """A restored file can be expired or from another account.

    "We wrote a file" is not evidence anybody is signed in, and this is the exact shape of
    mistake that reported a signed-out Codex as ready.
    """
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(CREDENTIAL))
    monkeypatch.setattr(
        "boss_engine.services.cli_harness.detect",
        lambda spec: {"installed": True, "authenticated": False, "auth_detail": "Not logged in"},
    )

    result = agent._authorise_from_file()

    assert result["authorised"] is False
    assert "did not sign" in result["reason"]


def test_a_working_credential_reports_signed_in(agent, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(CREDENTIAL))
    monkeypatch.setattr(
        "boss_engine.services.cli_harness.detect",
        lambda spec: {"installed": True, "authenticated": True,
                      "auth_detail": "Logged in using ChatGPT"},
    )

    assert agent._authorise_from_file()["authorised"] is True
