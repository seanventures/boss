"""A code agent's subprocess must not inherit the control plane's credentials.

On 2026-09-02 the agent was spawned with ``env={**os.environ, ...}``. It therefore held
DATABASE_URL for production. Its QA step ran the API test suite — the work it had been given —
and the suite cleared the tables it found. 204 of them, in the live system.

The lesson is not "agents should not run tests". Running tests is the job. The lesson is that
what a sandboxed process can reach has to be a decision somebody wrote down, not whatever
happened to be in the parent process.

These tests are the written-down version. The one that matters most is
`test_database_url_can_never_be_allowed_through`: no configuration change, no future edit to
the allow list, and no operator escape hatch may put that variable back.
"""

from __future__ import annotations

import pytest

from boss_engine.services.agent_env import (
    ALLOW_EXACT,
    EXTRA_ALLOW_VAR,
    NEVER,
    agent_environment,
    is_allowed,
)

#: The environment as it actually looked in the container that lost the data.
PRODUCTION_LIKE = {
    "PATH": "/usr/local/bin:/usr/bin",
    "HOME": "/home/boss",
    "LANG": "C.UTF-8",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-xxx",
    "DATABASE_URL": "postgresql+psycopg://boss:boss@postgres:5432/boss",
    "BOSS_GITHUB_TOKEN": "ghp_xxx",
    "BOSS_API_KEYS": "key1,key2",
    "OPS_TOKEN": "ops-xxx",
    "AWS_SECRET_ACCESS_KEY": "aws-xxx",
    "MEILI_MASTER_KEY": "meili-xxx",
    "OPENAI_API_KEY": "sk-xxx",
    "POSTGRES_PASSWORD": "hunter2",
}


class TestTheCredentialsThatCausedTheIncident:
    def test_database_url_does_not_reach_the_agent(self):
        env = agent_environment(PRODUCTION_LIKE)
        assert "DATABASE_URL" not in env

    @pytest.mark.parametrize("name", sorted(NEVER))
    def test_nothing_on_the_never_list_reaches_the_agent(self, name):
        env = agent_environment({**PRODUCTION_LIKE, name: "value"})
        assert name not in env

    def test_database_url_can_never_be_allowed_through(self):
        """The point of NEVER is that no later change can reopen the hole — not a new entry in
        the allow list, and not an operator setting the escape hatch."""
        source = {**PRODUCTION_LIKE, EXTRA_ALLOW_VAR: "DATABASE_URL,OPS_TOKEN"}
        env = agent_environment(source)
        assert "DATABASE_URL" not in env
        assert "OPS_TOKEN" not in env

    def test_the_never_list_is_not_merely_advisory(self):
        assert not is_allowed("DATABASE_URL", extra={"DATABASE_URL"})

    @pytest.mark.parametrize("name", ["BOSS_HOST_API_KEY", "BOSS_MCP_API_KEY", "BOSS_MASTER_KEY",
                                      "BOSS_SOME_FUTURE_API_KEY"])
    def test_boss_own_keys_never_reach_an_agent_even_when_an_operator_allows_them(self, name):
        """id-sec-final2: the host tools' and MCP keys were withheld only by SECRET_SHAPED, so
        BOSS_AGENT_ENV_ALLOW could pass them. They — and any BOSS_*API_KEY — are NEVER now."""
        env = agent_environment({**PRODUCTION_LIKE, name: "boss_sak_x", EXTRA_ALLOW_VAR: name})
        assert name not in env
        assert not is_allowed(name, extra={name})


class TestItStillLetsTheAgentWork:
    @pytest.mark.parametrize("name", ["PATH", "HOME", "LANG", "CLAUDE_CODE_OAUTH_TOKEN"])
    def test_what_the_agent_needs_survives(self, name):
        assert name in agent_environment(PRODUCTION_LIKE)

    def test_ci_survives_for_noninteractive_builds(self):
        assert agent_environment(PRODUCTION_LIKE, {"CI": "1"})["CI"] == "1"

    def test_the_model_credential_is_allowed_deliberately_and_in_writing(self):
        """It is credential-shaped and it is allowed anyway: it buys model turns, not access to
        anybody's data. That distinction is the whole basis of the list, so it is asserted."""
        assert "CLAUDE_CODE_OAUTH_TOKEN" in ALLOW_EXACT
        assert "ANTHROPIC_API_KEY" in ALLOW_EXACT

    def test_the_shared_tool_caches_still_get_through(self):
        env = agent_environment(
            PRODUCTION_LIKE,
            {"npm_config_cache": "/cache/npm", "PLAYWRIGHT_BROWSERS_PATH": "/cache/pw"},
        )
        assert env["npm_config_cache"] == "/cache/npm"
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/cache/pw"

    def test_overrides_are_filtered_too(self):
        """Otherwise the override argument becomes a way around the whole thing."""
        env = agent_environment(PRODUCTION_LIKE, {"DATABASE_URL": "postgres://sneaky"})
        assert "DATABASE_URL" not in env


class TestUnknownVariablesAreDeniedNotGuessed:
    @pytest.mark.parametrize("name", [
        "SOME_NEW_SERVICE_PASSWORD", "CUSTOMER_DB_SECRET", "STRIPE_API_KEY",
        "INTERNAL_TOKEN", "X_PRIVATE_KEY",
    ])
    def test_credential_shaped_names_are_withheld_even_if_nobody_listed_them(self, name):
        assert not is_allowed(name)

    def test_an_unrecognised_variable_is_withheld(self):
        """Deny by default. The next dangerous variable has not been invented yet, and this is
        the only rule that covers it."""
        assert not is_allowed("SOME_FUTURE_THING")
        assert "SOME_FUTURE_THING" not in agent_environment(
            {**PRODUCTION_LIKE, "SOME_FUTURE_THING": "x"}
        )

    def test_an_operator_can_still_add_something_harmless(self):
        source = {**PRODUCTION_LIKE, "MY_TOOL_MODE": "fast", EXTRA_ALLOW_VAR: "MY_TOOL_MODE"}
        assert agent_environment(source)["MY_TOOL_MODE"] == "fast"


class TestEverySpawnPointGoesThroughIt:
    """A single unfiltered spawn puts the credentials back."""

    def test_the_code_agent_subprocess_is_filtered(self):
        from pathlib import Path

        source = Path("engine/boss_engine/services/code_agent.py").read_text()
        assert "agent_environment(os.environ" in source
        assert "env={**os.environ" not in source, (
            "a spawn that inherits os.environ hands the agent production credentials again"
        )

    def test_sandbox_validation_is_filtered(self):
        from pathlib import Path

        source = Path("engine/boss_engine/services/sandbox_validation.py").read_text()
        assert "agent_environment(os.environ" in source
        assert "env={**os.environ" not in source, (
            "this runs the project's own test command; unfiltered, `make test` in a sandbox "
            "points at the production database"
        )

    def test_native_sandbox_run_filters_the_real_child(self, tmp_path, monkeypatch):
        from boss_engine.config import Settings
        from boss_engine.services.sandbox import EphemeralSandbox

        monkeypatch.setenv("DATABASE_URL", "postgresql://production-secret")
        sandbox = EphemeralSandbox(Settings(), task_id="credential_filter")
        sandbox.path = tmp_path
        result = sandbox.run(
            "python3 -c \"import os; print(os.getenv('DATABASE_URL', 'ABSENT'), os.getenv('CI'))\"")
        assert result.exit_code == 0, result.stderr
        assert "ABSENT 1" in result.stdout

    def test_codex_chat_restores_login_without_inheriting_database(self, monkeypatch):
        from boss_engine.config import Settings
        from boss_engine.services.codex_cli_adapter import CodexCliAdapter

        calls = []
        monkeypatch.setenv("DATABASE_URL", "postgresql://production-secret")
        monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
        monkeypatch.setattr(
            "boss_engine.services.cli_harness.GenericCliCodeAgent.ensure_authorised",
            lambda self: calls.append("restored") or {"authorised": True})
        env = CodexCliAdapter(Settings())._child_env()
        assert calls == ["restored"]
        assert env["CODEX_HOME"] == "/tmp/codex-home"
        assert "DATABASE_URL" not in env

    def test_codex_harness_and_login_probe_withhold_control_plane_keys(self, monkeypatch):
        from subprocess import CompletedProcess

        from boss_engine.config import Settings
        from boss_engine.services.cli_harness import (
            BUILTIN_CLI_HARNESSES, GenericCliCodeAgent, _run_probe,
        )

        monkeypatch.setenv("DATABASE_URL", "postgresql://production-secret")
        monkeypatch.setenv("OPENAI_API_KEY", "control-plane-model-key")
        monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
        agent = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])
        child_env = agent._env()
        assert "DATABASE_URL" not in child_env
        assert "OPENAI_API_KEY" not in child_env
        assert child_env["CODEX_HOME"] == "/tmp/codex-home"

        captured = {}

        def run(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr("boss_engine.services.cli_harness.owned_process.run", run)
        assert _run_probe("codex", ("--version",))[0] == 0
        assert "DATABASE_URL" not in captured["env"]
        assert "OPENAI_API_KEY" not in captured["env"]
        assert captured["env"]["CODEX_HOME"] == "/tmp/codex-home"

    def test_claude_drivers_receive_only_their_model_token(self, monkeypatch):
        from boss_engine.config import Settings
        from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
        from boss_engine.services.code_agent import ClaudeCodeAgent

        monkeypatch.setenv("DATABASE_URL", "postgresql://production-secret")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        settings = Settings(CLAUDE_CODE_OAUTH_TOKEN="wizard-stored-token")
        for child in (ClaudeCliAdapter(settings), ClaudeCodeAgent(settings)):
            env = child._child_env()
            assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "wizard-stored-token"
            assert "DATABASE_URL" not in env


class TestTheConsoleShellIsFilteredToo:
    """The other spawn point, which already believed it was safe.

    agent_shell dropped anything whose NAME matched TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL and
    described itself as giving the child "no BOSS credentials to find". DATABASE_URL matches
    none of those words, so the single most dangerous variable in the environment — password
    included, inside the URL — was the one that filter passed through.

    A denylist has to predict the next dangerous name. This is why the rule is deny-by-default
    everywhere, not just where the incident happened to land.
    """

    def test_the_shell_uses_the_shared_allow_list(self):
        from pathlib import Path

        source = Path("engine/boss_engine/services/agent_shell.py").read_text()
        assert "agent_environment(" in source

    def test_the_old_name_denylist_is_gone(self):
        from pathlib import Path

        source = Path("engine/boss_engine/services/agent_shell.py").read_text()
        assert "TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL" not in source.replace("\n", " ") or \
            "agent_environment(" in source

    def test_a_database_url_would_not_have_survived_the_old_filter_either(self):
        """The regression this encodes: DATABASE_URL is invisible to a name-pattern denylist."""
        import re

        assert not re.search(r"(TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL)", "DATABASE_URL", re.I)
        assert not is_allowed("DATABASE_URL")

    def test_the_shell_still_gets_what_it_needs_to_clone(self):
        env = agent_environment(
            PRODUCTION_LIKE, {"HOME": "/tmp/ws", "GIT_TERMINAL_PROMPT": "0"},
        )
        assert env["HOME"] == "/tmp/ws"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "PATH" in env
