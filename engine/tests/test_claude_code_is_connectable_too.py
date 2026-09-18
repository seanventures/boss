""""I choose claude code for few roles.. But looks like I could connect only one model."

Codex could be detected, connected and proved from the console. Claude Code — the engine most
of this product actually runs on — had prose: set CLAUDE_CODE_OAUTH_TOKEN on the API container
and restart it. That is the one step the wizard exists to remove, and it left the operator who
wanted Claude Code for the reviewer role with no way to choose it, because nothing had recorded
it as connected.

The gap was that a CliHarnessSpec did two jobs at once: it described how to DRIVE a CLI, and it
described how to FIND and AUTHORISE one. Claude Code already had a driver — a purpose-written
adapter a governed task has completed on — so it had no spec, so it had no detection either.
Those two jobs are now separable.
"""
from __future__ import annotations

import re

from boss_engine.services.cli_harness import (
    BUILTIN_CLI_HARNESSES,
    _uses_env_route,
)


CLAUDE = BUILTIN_CLI_HARNESSES["claude_code"]


def test_claude_code_has_a_spec_so_the_console_can_find_it():
    assert CLAUDE.binary == "claude"
    assert CLAUDE.entitlement_source_id == "anthropic_claude_code"
    assert CLAUDE.entitlement_provider == "anthropic"


def test_but_it_does_not_take_over_driving_claude_code():
    """ClaudeCodeAgent is the better driver and the verified one. A generic `claude -p`
    wrapper registered under the same name would replace it with something worse — or collide
    at import, which is the correct outcome but not a shipping product."""
    from boss_engine.services.harness_registry import get_harness

    assert CLAUDE.registers_harness is False
    assert get_harness("claude_code").build.__name__ == "ClaudeCodeAgent"
    assert get_harness("claude_code").verified is True


def test_the_other_specs_still_register_their_own_harness():
    for name in ("codex", "cursor", "aider"):
        assert BUILTIN_CLI_HARNESSES[name].registers_harness is True


def test_the_variable_is_the_login_so_there_is_nothing_to_sign_in():
    """Verified against claude 2.1.273: with CLAUDE_CODE_OAUTH_TOKEN set and nothing on disk,
    `claude auth status` reports loggedIn true. There is no login subcommand to feed and no
    file to place — which is exactly why Claude never had codex's two-machines problem."""
    assert CLAUDE.token_env == "CLAUDE_CODE_OAUTH_TOKEN"
    assert CLAUDE.token_login_args == ()
    assert CLAUDE.auth_file_secret == ""
    assert _uses_env_route(CLAUDE) is True


def test_and_codex_is_still_the_file_route():
    """The routes are told apart, not guessed at: codex's whole auth.json has to travel."""
    codex = BUILTIN_CLI_HARNESSES["codex"]
    assert _uses_env_route(codex) is False
    assert codex.auth_file_secret == "CODEX_AUTH_JSON"


def test_the_auth_check_reads_a_field_not_a_sentence():
    """`claude auth status` answers in JSON, so the check cannot be broken by a reworded
    message — and "loggedIn": false cannot match the affirmative the way "Not logged in" did."""
    assert CLAUDE.auth_check_args == ("auth", "status")
    signed_in = '{"loggedIn": true, "authMethod": "oauth_token"}'
    signed_out = '{"loggedIn": false, "authMethod": "none"}'
    assert re.search(CLAUDE.auth_ok_pattern, signed_in)
    assert not re.search(CLAUDE.auth_fail_pattern, signed_in)
    assert re.search(CLAUDE.auth_fail_pattern, signed_out)


def test_proving_the_connection_cannot_touch_a_file():
    """Connecting runs one real turn, because signed in is not the same as working. `--tools
    ""` is the CLI's own way to disable every tool, so the proof can read a model and nothing
    else."""
    assert CLAUDE.read_only_args == ("--tools", "")
    assert CLAUDE.supports_read_only() is True


def test_a_stored_token_reaches_the_cli_on_every_run():
    """Nothing is written to disk, so the credential has to be in the child's environment —
    including the credential BOSS stored, which is not in this process's environment at all."""
    import inspect

    from boss_engine.services import cli_harness

    source = inspect.getsource(cli_harness.GenericCliCodeAgent._env)
    assert "_uses_env_route(self.spec)" in source
    assert "self._resolve_secret(self.spec.token_env)" in source


def test_and_detection_sees_it_too():
    """Otherwise the operator who has just handed the token over is told it did not arrive:
    the probe runs in a process whose environment does not contain what BOSS stored."""
    import inspect

    from boss_engine.services import cli_harness

    source = inspect.getsource(cli_harness.detect)
    assert "extra_env=probe_env" in source
    assert "_resolve_secret(spec.token_env)" in source
    # And `detect_all` supplies the settings that lookup needs.
    assert "detect(spec, settings)" in inspect.getsource(cli_harness.detect_all)


def test_the_token_lookup_finds_what_boss_itself_stored():
    """The old copy read the store under the plain key only, so a token written by BOSS's own
    API — which namespaces what it writes — was invisible to the one path that needed it."""
    import inspect

    from boss_engine.services import cli_harness

    source = inspect.getsource(cli_harness.GenericCliCodeAgent._resolve_token)
    assert "_resolve_secret(self.spec.token_env)" in source


def test_the_stored_reference_names_its_provider():
    """The runtime resolves a lane's credential BY PROVIDER — anthropic for claude_code — and
    put_secret records the secret kind in that column. Without the fix-up the token is stored
    successfully and then never found."""
    import inspect

    from boss_engine.services import setup_service

    source = inspect.getsource(setup_service.store_coding_agent_credential)
    assert "UPDATE secret_refs SET provider" in source
    assert "spec.entitlement_provider" in source


def test_the_status_is_reported_as_a_sentence_not_as_json():
    """`claude auth status` answers in JSON, which is the right thing to TEST against and the
    wrong thing to show: the most informative line is literally `"loggedIn": false,` — a
    fragment, on the screen where a customer is working out whether their plan is connected."""
    from boss_engine.services.cli_harness import _auth_detail

    assert _auth_detail(CLAUDE, '{"loggedIn": false, "authMethod": "none"}') == "Not signed in."
    assert _auth_detail(CLAUDE, '{"loggedIn": true, "authMethod": "oauth_token"}') == (
        "Signed in with a long-lived token.")
    assert _auth_detail(
        CLAUDE,
        '{"loggedIn": true, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}',
    ) == "Signed in with an API key (ANTHROPIC_API_KEY)."


def test_and_a_cli_that_speaks_prose_is_left_alone():
    """The troubleshooting tables in the setup doc are keyed on codex's own words."""
    from boss_engine.services.cli_harness import _auth_detail

    codex = BUILTIN_CLI_HARNESSES["codex"]
    assert _auth_detail(codex, "Logged in using ChatGPT") == "Logged in using ChatGPT"
    assert _auth_detail(codex, "WARNING: could not create PATH aliases\nNot logged in") == (
        "Not logged in")


def test_the_prompt_travels_on_stdin_because_tools_is_variadic():
    """MEASURED ON THE FIRST REAL CONNECT, and the failure was mine:

        claude -p --tools "" "Reply with exactly: BOSS_OK"
        Error: Input must be provided either through stdin or as a prompt argument

    `--tools <tools...>` is variadic, so it consumes every argument after it — including a
    prompt appended last. The credential was stored and the CLI was signed in; the proof turn
    could not be given anything to do, which reads as "your subscription does not work".
    """
    assert CLAUDE.prompt_via == "stdin"
    # The trap is the combination, so the guard is too: any spec whose last read-only
    # argument is a variadic flag's value cannot append its prompt to argv.
    assert CLAUDE.read_only_args[0] == "--tools"
