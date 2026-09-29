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


# --- the console's Connect button, on a server ------------------------------


def test_connect_tries_the_stored_credential_before_refusing() -> None:
    """On a server the CLI is NEVER signed in by hand — nobody can open a browser there.

    So "not signed in" is the starting state, not a verdict. Refusing on it made the console's
    Connect button permanently unusable on exactly the deployments it exists for, and told the
    operator to run `codex login` on a machine they cannot log into. Measured: the deployed API
    answered "installed but not signed in" while a valid credential sat in its environment.

    Checked structurally, because the alternative — mocking detect and ensure_authorised, then
    asserting the mock ran — tests the mocks. What matters is the ORDER of two real statements:
    the restore attempt has to come before the refusal.
    """
    import inspect

    from boss_engine.services.setup_service import connect_coding_agent

    source = inspect.getsource(connect_coding_agent)
    restore_at = source.find("ensure_authorised")
    refuse_at = source.find("installed but not signed in")

    assert restore_at != -1, "connect never tries the stored credential"
    assert restore_at < refuse_at, (
        "connect refuses before trying the stored credential, so a server can never connect")


def test_a_run_authorises_a_file_route_harness_too(monkeypatch, tmp_path) -> None:
    """The guard keyed off token_env, which codex no longer has.

    Left as it was, a harness on the file route would never restore its credential before a
    build, and every run would fail as "not signed in" on a deployment that had the credential.
    """
    import inspect

    from boss_engine.services import cli_harness

    source = inspect.getsource(cli_harness.GenericCliCodeAgent.run_role)
    assert "auth_file_secret" in source, (
        "run_role still gates authorisation on token_env alone, so the file route never runs")


# --- a stale credential file must not outrank the store ----------------------


def test_a_newer_stored_credential_replaces_a_stale_file(agent, monkeypatch) -> None:
    """THE bug a customer hit. "Signed in" is not "signed in with the right credential".

    A file written by an earlier run makes the CLI report signed in, so authorisation used to
    stop there and never write the newer credential. Measured on a real install: the CLI said
    "Logged in using ChatGPT" while every turn failed 401, because the laptop that minted the
    token had since rotated it — and re-sending a fresh one, exactly as the docs instruct,
    changed nothing at all.
    """
    agent._write_auth_file(json.dumps({"tokens": {"access_token": "stale"}}))
    fresh = json.dumps({"tokens": {"access_token": "fresh"}})
    monkeypatch.setenv("CODEX_AUTH_JSON", fresh)
    monkeypatch.setattr(
        "boss_engine.services.cli_harness.detect",
        lambda spec: {"installed": True, "authenticated": True,
                      "auth_detail": "Logged in using ChatGPT"},
    )

    result = agent._authorise_from_file(cli_reports_signed_in=True)

    assert result["authorised"] is True
    assert json.loads(agent._current_auth_file()) == json.loads(fresh), (
        "the stale file survived, so re-sending a credential does nothing")


def test_an_unchanged_credential_is_not_rewritten(agent, monkeypatch) -> None:
    """Rewriting the same bytes proves nothing and touches a live credential for no reason."""
    same = json.dumps(CREDENTIAL)
    agent._write_auth_file(same)
    monkeypatch.setenv("CODEX_AUTH_JSON", same)

    result = agent._authorise_from_file(cli_reports_signed_in=True)

    assert result["reason"] == "already signed in from the stored credential"


def test_a_hand_signed_in_cli_is_never_clobbered(agent, monkeypatch) -> None:
    """Path B: BOSS runs where the operator signed in, and nothing is stored.

    Overwriting here would take away the only credential they have — and there is nothing to
    overwrite it WITH, which is worse.
    """
    agent._write_auth_file(json.dumps({"tokens": {"access_token": "theirs"}}))
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    monkeypatch.setattr(type(agent), "_resolve_secret", lambda self, name: None)

    result = agent._authorise_from_file(cli_reports_signed_in=True)

    assert result["authorised"] is True
    assert json.loads(agent._current_auth_file())["tokens"]["access_token"] == "theirs"


def test_authorisation_consults_the_store_before_trusting_the_cli() -> None:
    """Structural: the file route must run BEFORE the "already signed in" shortcut.

    Checked by order rather than by mocking, because the defect was precisely that one branch
    returned before the other could run.
    """
    import inspect

    from boss_engine.services import cli_harness

    source = inspect.getsource(cli_harness.GenericCliCodeAgent.ensure_authorised)
    file_route_at = source.find("return self._authorise_from_file(")
    # The RETURN, not any mention: the prose explaining this ordering also contains the phrase,
    # and matching that made the test pass or fail on where a comment sat.
    shortcut_at = source.find('return {"authorised": True, "reason": "already signed in"}')

    assert file_route_at != -1 and shortcut_at != -1
    assert file_route_at < shortcut_at, (
        "the signed-in shortcut runs first, so a stale file blocks the stored credential")


# --- BOSS stores it, so the customer needs no store of their own -------------


def test_the_harness_finds_what_bosss_own_api_stored(monkeypatch, tmp_path) -> None:
    """The join that makes the one-command route work.

    `put_secret` namespaces keys as BOSS_<SCOPE>_<FIELD> so a shared store stays readable, so a
    credential BOSS stored itself is NOT under the plain name. A reader that looked only under
    the plain name would miss everything the product put there — the credential would be
    stored successfully and never found, which is the worst of both.
    """
    from boss_engine.services.cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from boss_engine.services.secrets_service import ref_key_for

    namespaced = ref_key_for(SUBSCRIPTION_SECRET_SCOPE, "CODEX_AUTH_JSON")
    assert namespaced == "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON"

    class OnlyNamespaced:
        def get(self, key):
            if key != namespaced:
                raise KeyError(key)
            return json.dumps(CREDENTIAL)

    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    monkeypatch.setattr("boss_engine.services.secret_backends.default_secret_backend",
                        lambda settings: "database")
    monkeypatch.setattr("boss_engine.services.secret_backends.backend_for",
                        lambda kind, settings: OnlyNamespaced())

    agent = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])
    assert json.loads(agent._resolve_secret("CODEX_AUTH_JSON")) == CREDENTIAL


def test_an_operator_set_variable_still_wins(monkeypatch) -> None:
    """The plain name must keep working; people set it by hand while working things out."""
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(CREDENTIAL))

    agent = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])
    assert json.loads(agent._resolve_secret("CODEX_AUTH_JSON")) == CREDENTIAL


def test_storing_refuses_a_claude_session_token() -> None:
    """The setup-token import has no recorded counsel clearance, so it cannot be stored."""
    from boss_engine.services.setup_service import SetupError, store_coding_agent_credential

    token = "sk-ant-oat01-session-token-material"
    with pytest.raises(SetupError) as exc:
        store_coding_agent_credential(None, Settings(), "p", "claude_code", token, actor="t")
    assert "disabled" in str(exc.value)
    assert "clearance" in str(exc.value)
    assert token not in str(exc.value)


def test_storing_refuses_a_provider_that_cannot_travel() -> None:
    """Cursor has no credential that works off its own machine; accepting one would lie."""
    from boss_engine.services.setup_service import SetupError, store_coding_agent_credential

    with pytest.raises(SetupError) as exc:
        store_coding_agent_credential(None, Settings(), "p", "cursor", "{}", actor="t")
    assert "no credential that travels" in str(exc.value)


def test_storing_refuses_malformed_json_without_quoting_it() -> None:
    from boss_engine.services.setup_service import SetupError, store_coding_agent_credential

    with pytest.raises(SetupError) as exc:
        store_coding_agent_credential(
            None, Settings(), "p", "codex", '{"tokens": "super-secret-value"', actor="t")
    assert "not valid JSON" in str(exc.value)
    assert "super-secret-value" not in str(exc.value)


def test_storing_refuses_an_empty_credential() -> None:
    from boss_engine.services.setup_service import SetupError, store_coding_agent_credential

    with pytest.raises(SetupError) as exc:
        store_coding_agent_credential(None, Settings(), "p", "codex", "   ", actor="t")
    assert "required" in str(exc.value)
