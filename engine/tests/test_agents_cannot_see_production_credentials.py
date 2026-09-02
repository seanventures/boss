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


class TestItStillLetsTheAgentWork:
    @pytest.mark.parametrize("name", ["PATH", "HOME", "LANG", "CLAUDE_CODE_OAUTH_TOKEN"])
    def test_what_the_agent_needs_survives(self, name):
        assert name in agent_environment(PRODUCTION_LIKE)

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
