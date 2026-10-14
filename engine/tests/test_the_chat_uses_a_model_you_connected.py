""""I've configured the claude and codex right? why the agent did not work"

Because nothing had chosen between them. What the operator saw, having connected two working
subscriptions minutes earlier:

    IntegrationConfigurationError: OPENAI_API_KEY is not configured.. This is a failure on my
    side, not a limit on what BOSS knows

True, and useless. The CHAT adapters for both CLIs already existed — `ClaudeCliAdapter`,
`make_codex_chat_adapter` — and the selection went: `chat_llm_engine` is empty, so inherit
`llm_engine`, which defaults to "auto", which is the OpenAI adapter. An install with two
subscriptions and no API key therefore had a chat that could not answer, and a Setup screen
that said everything was connected.

Two fixes, and the second is the one that matters: the choice is now a knob in the console,
AND an unset choice resolves to a subscription that IS connected rather than to an API key
that is not.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.project_settings import (
    CHAT_ENGINES,
    KNOBS,
    _allowed_choices,
    offered_choices,
)
from boss_engine.services.setup_service import chat_engine_for


class _Settings:
    openai_api_key = ""
    anthropic_api_key = ""
    claude_code_oauth_token = ""
    azure_openai_key = ""
    openrouter_api_key = ""
    groq_api_key = ""
    deepseek_api_key = ""
    vllm_base_url = ""
    ollama_base_url = ""
    cli_harnesses_json = ""
    chat_llm_engine = ""
    llm_engine = "auto"


@pytest.fixture()
def connection(entitlement_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
                entitlement_tables(conn)
    with engine.begin() as conn:
        yield conn


def _connect(conn, source_id):
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider,"
        " connection_status, credential_secret_ref_id, updated_at)"
        " VALUES ('p', :s, 'x', 'connected', NULL, :at)"),
        {"s": source_id, "at": datetime(2026, 9, 18, tzinfo=timezone.utc)})


def _choose(conn, value):
    conn.execute(text("INSERT INTO project_settings (project_id, key, value)"
                      " VALUES ('p', 'chat_llm_engine', :v)"), {"v": value})


def test_a_connected_subscription_answers_the_chat(connection):
    """THE BUG. Claude Code was connected and proved with a real turn; the chat asked for an
    OpenAI key."""
    _connect(connection, "anthropic_claude_code")

    assert chat_engine_for(connection, _Settings(), "p") == "claude_code"


def test_codex_will_do_it_too_when_claude_is_not_there(connection):
    _connect(connection, "openai_codex_chatgpt")

    assert chat_engine_for(connection, _Settings(), "p") == "codex"


def test_claude_first_when_both_are_connected(connection):
    """Not a preference about quality: Claude is the one whose subscription mints a headless
    token designed for this, so it is the one that keeps working unattended."""
    _connect(connection, "anthropic_claude_code")
    _connect(connection, "openai_codex_chatgpt")

    assert chat_engine_for(connection, _Settings(), "p") == "claude_code"


def test_a_connected_subscription_beats_a_stale_environment_key(connection):
    """Repo and models' one resolver (model_routing.resolve_job): a key in the install's
    environment answers only when nothing is connected. An operator who connects a
    subscription in the console is never overridden by a leftover OPENAI_API_KEY — Claude
    Code's "stale key overrides subscription" trap, avoided."""
    class _WithKey(_Settings):
        openai_api_key = "sk-x"

    _connect(connection, "anthropic_claude_code")

    assert chat_engine_for(connection, _WithKey(), "p") == "claude_code"


def test_an_install_with_only_an_environment_key_keeps_it(connection):
    """Nothing connected: the environment decides, exactly as before this existed."""
    class _WithKey(_Settings):
        openai_api_key = "sk-x"

    assert chat_engine_for(connection, _WithKey(), "p") == ""


def test_the_console_choice_beats_everything(connection):
    """The project's choice comes first while it is connected — ahead of Auto's preference
    for Claude and of the install's environment key."""
    class _WithKey(_Settings):
        openai_api_key = "sk-x"

    _connect(connection, "anthropic_claude_code")
    _connect(connection, "openai_codex_chatgpt")
    _choose(connection, "codex")

    assert chat_engine_for(connection, _WithKey(), "p") == "codex"


def test_and_the_deployment_setting_beats_the_guess(connection):
    class _Configured(_Settings):
        chat_llm_engine = "native"

    _connect(connection, "anthropic_claude_code")

    assert chat_engine_for(connection, _Configured(), "p") == "native"


def test_nothing_connected_changes_nothing(connection):
    """Silence, so the adapter behaves exactly as it did before this existed — including its
    own error message, which is the honest one when there is genuinely no model."""
    assert chat_engine_for(connection, _Settings(), "p") == ""


def test_the_console_offers_only_engines_that_can_hold_a_conversation(connection):
    """Every engine the router can build a chat adapter for (model_routing.adapter_for builds
    each on its own credential and endpoint), and no coding-only harness."""
    from boss_engine.services.model_routing import CHAT_ROUTABLE

    assert CHAT_ENGINES == CHAT_ROUTABLE
    assert _allowed_choices(KNOBS["chat_llm_engine"]) == list(CHAT_ENGINES)
    assert "aider" not in _allowed_choices(KNOBS["chat_llm_engine"])


def test_and_narrows_them_to_what_is_connected(connection):
    _connect(connection, "openai_codex_chatgpt")

    assert offered_choices(connection, _Settings(), "p", KNOBS["chat_llm_engine"]) == ["codex"]


def test_the_console_vocabulary_reaches_the_factory(connection):
    """`claude_code` is the name in the role pickers, in detection and on the Connect button;
    this factory has always spelled it `claude_cli`. Without the alias the operator's choice
    lands in the `!= "claude_cli"` branch and gets the OpenAI adapter — the exact failure."""
    from boss_engine.services.claude_cli_adapter import _chat_engine_name

    assert _chat_engine_name("claude_code") == "claude_cli"
    assert _chat_engine_name("codex") == "codex"
    assert _chat_engine_name("native") == "native"


def test_the_router_resolves_it_once_per_turn():
    """The answerer is cached, so the engine has to be cached with it — and a change in the
    console has to rebuild it on the next turn, not at the next restart."""
    import inspect

    from boss_engine.services import intent_router

    turn = inspect.getsource(intent_router.IntentRouter.route_turn)
    assert "self._use_project_chat_model(connection, project_id)" in turn

    apply = inspect.getsource(intent_router.IntentRouter._use_project_chat_model)
    assert "model_routing.choose(" in apply
    assert "self._answer_agent_obj = None" in apply


def test_the_chat_cli_is_given_the_credential_this_install_holds():
    """THE SECOND HALF OF THE SAME BUG, and the one that made the first invisible.

    Selecting Claude was not enough: the adapter ran `claude` with no environment at all, so
    the child inherited the API container's — which on an install that keeps its token in a
    secret store is empty. `claude -p` said "Not logged in", the falling-back adapter
    correctly read that as "this backend is unavailable here", and handed the operator the
    SECOND backend's error about a subscription that was connected, proved and stored.
    """
    import inspect

    from boss_engine.services import claude_cli_adapter, codex_cli_adapter

    from boss_engine.services import cli_harness

    invoke = inspect.getsource(claude_cli_adapter.ClaudeCliAdapter._invoke_once)
    assert "env=self._child_env()" in invoke

    env = inspect.getsource(claude_cli_adapter.ClaudeCliAdapter._child_env)
    # The one resolution every `claude` child uses: the login, or the console's Anthropic API
    # key (claude_credential, BOSSAPP-530) — and it asks the install's store.
    assert "resolve_claude_credential(self.settings)" in env
    from boss_engine.services import claude_credential

    assert "_store_lookup(settings, TOKEN_NAME)" in inspect.getsource(
        claude_credential._subscription_token)
    assert "stored_credential(" in inspect.getsource(claude_credential._store_lookup)
    assert cli_harness.claude_login_token  # kept as a view of the same resolution

    # Codex travels as a FILE, so the fix is to place it, not to pass a token.
    codex = inspect.getsource(codex_cli_adapter.CodexCliAdapter._child_env)
    assert "ensure_authorised()" in codex


def test_and_one_resolver_serves_both_the_builder_and_the_talker():
    """Two copies of "environment, then the store, under either key" is how the chat came to
    have none."""
    import inspect

    from boss_engine.services import cli_harness

    assert "stored_credential(self.settings, name)" in inspect.getsource(
        cli_harness.GenericCliCodeAgent._resolve_secret)


def test_and_the_environment_still_wins_over_the_store(monkeypatch):
    """What an operator exports while debugging must not be silently overridden — measured by
    setting it, rather than by reading the order of two lines."""
    from boss_engine.config import get_settings
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-from-the-environment")

    def _never(*_a, **_k):  # a store read here would mean the environment lost
        raise AssertionError("the store was consulted while the environment was set")

    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential", _never)

    env = ClaudeCliAdapter(get_settings())._child_env()

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-from-the-environment"


def test_and_the_code_agent_gets_it_too():
    """"claude code agent exited 1: Not logged in · Please run /login" — on a governed run,
    about a subscription that was connected, proved and stored.

    Same gap as the chat adapter, one layer down and found later because a build is the
    slower thing to try. `agent_environment` decides what may be INHERITED and denies by
    default, which is right; the token is not inherited on an install that keeps credentials
    in a secret store, so it has to be added the way the tool caches are — as something BOSS
    resolved rather than something the process was started with.
    """
    import inspect

    from boss_engine.services.code_agent import ClaudeCodeAgent

    invoke = inspect.getsource(ClaudeCodeAgent._invoke)
    assert "env=self._child_env()" in invoke

    env = inspect.getsource(ClaudeCodeAgent._child_env)
    # The allowlist still decides what is inherited — this adds one value, it does not widen.
    assert "agent_environment(os.environ, _shared_tool_caches())" in env
    # The one resolution every `claude` child uses (claude_credential.resolve_claude_credential).
    # (through the one hook an engine may override: claude_agent_sdk asks for the key only).
    assert "self._resolve_credential()" in env
    assert "resolve_claude_credential(self.settings)" in inspect.getsource(
        ClaudeCodeAgent._resolve_credential)
    from boss_engine.services import claude_credential

    resolve = inspect.getsource(claude_credential._subscription_token)
    # And what the lane binder already resolved is preferred over asking the store again.
    assert resolve.index("claude_code_oauth_token") < resolve.index("_store_lookup(")


def test_and_an_operator_set_token_still_wins(monkeypatch):
    """What somebody exports while debugging must not be overridden by a store — the same rule
    the chat path follows."""
    from boss_engine.config import Settings
    from boss_engine.services.code_agent import ClaudeCodeAgent

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-token")

    def _never(*_args, **_kwargs):
        raise AssertionError("the store was consulted while an operator token was set")

    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential", _never)
    env = ClaudeCodeAgent(Settings(CLAUDE_CODE_OAUTH_TOKEN=""))._child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "operator-token"


def test_a_project_bound_token_wins_over_the_host_environment(monkeypatch):
    """A project connection must not silently use another project's host credential."""
    from boss_engine.config import Settings
    from boss_engine.services.code_agent import ClaudeCodeAgent

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-api-key")
    env = ClaudeCodeAgent(Settings(CLAUDE_CODE_OAUTH_TOKEN="project-token"))._child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "project-token"
    assert "ANTHROPIC_API_KEY" not in env
