"""The last form of the single-model dependency was the DEFAULT.

"The main objective of BOSS is not to depend on a single model but must be able to connect and
work with any model provider."

The seam itself has been provider-agnostic for a while: one `run_role` interface, a registry
of eleven harnesses, and an engine-native tool-calling agent — read_file, glob, grep,
edit_file, write_file, bash, sandbox-confined — for any OpenAI-compatible key. Verified by
running it: on an OpenRouter key, with no Claude or Codex involved, it read a repository, made
a two-tool-call surgical edit and turned a failing suite green.

What was left was that `code_agent_engine` ships as "claude_code" and EVERY fallback chain
ended at that literal string. So an install that connected only an OpenRouter key got a build
that shelled out to a `claude` binary it does not have, and the failure read as a broken
pipeline rather than as a default nobody had chosen.

The fix speaks only when the configured engine cannot run here, so an install with Claude
Code connected behaves exactly as before.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.setup_service import build_engine_for


class _Settings:
    code_agent_engine = "claude_code"      # the shipped default
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


@pytest.fixture()
def connection(entitlement_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
                entitlement_tables(conn, with_settings=False)
    with engine.begin() as conn:
        yield conn


def _connect(conn, source_id, *, ref=None):
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider,"
        " connection_status, credential_secret_ref_id, updated_at)"
        " VALUES ('p', :s, 'x', :cs, :ref, :at)"),
        {"s": source_id, "cs": "not_connected" if ref else "connected", "ref": ref,
         "at": datetime(2026, 9, 18, tzinfo=timezone.utc)})


def test_an_install_with_only_an_api_key_does_not_reach_for_claude(connection):
    """THE BUG. Nothing about this install can run the Claude CLI, and the default said to."""
    _connect(connection, "setup-openrouter", ref="sref_1")

    assert build_engine_for(connection, _Settings(), "p") == "openrouter"


def test_an_install_with_claude_connected_is_left_alone(connection):
    """The conservative half: no behaviour change for anybody who works today."""
    _connect(connection, "anthropic_claude_code")

    assert build_engine_for(connection, _Settings(), "p") == "claude_code"


def test_an_explicit_choice_is_never_second_guessed(connection):
    class _Chose(_Settings):
        code_agent_engine = "codex"

    _connect(connection, "openai_codex_chatgpt")
    _connect(connection, "anthropic_claude_code")

    assert build_engine_for(connection, _Chose(), "p") == "codex"


def test_a_proven_harness_is_preferred_over_a_declared_one(connection):
    """`verified` means a governed task has actually completed on it. Choosing an unproven
    adapter BY DEFAULT would present a hypothesis as a decision."""
    _connect(connection, "cursor_subscription")        # verified=False in the registry
    _connect(connection, "setup-openrouter", ref="s2")  # a verified native lane

    assert build_engine_for(connection, _Settings(), "p") == "openrouter"


def test_nothing_connected_leaves_the_error_where_it_was(connection):
    """With no credentials at all there is nothing to choose, and inventing an answer would
    replace a clear "not signed in" with a confusing one about a different provider."""
    assert build_engine_for(connection, _Settings(), "p") == "claude_code"


def test_the_pipeline_actually_asks(connection):
    import inspect

    from boss_engine.services import governed_code_task_service as g
    from boss_engine.services.model_router import stages

    assert "lane_engines(_c, settings, project_id, spec)" in inspect.getsource(g)
    source = inspect.getsource(stages.lane_engines)
    assert "_build_engine_for(connection, settings, project_id)" in source
    assert "_pinned_engine or _resolved_engine or settings.code_agent_engine" in source


def test_a_pinned_engine_is_checked_against_the_registry_not_a_list_of_three():
    """The old allow-list — claude_code, openai, native — predated the harness seam and
    silently dropped every engine added since: a run pinned to "codex:gpt-5-codex" in string
    form fell back to the process default and ran somewhere else with nothing saying so."""
    import inspect

    from boss_engine.services import governed_code_task_service as g
    from boss_engine.services.model_router import stages

    assert '("claude_code", "openai", "native")' not in inspect.getsource(g)
    source = inspect.getsource(stages.pinned_engine)
    assert '("claude_code", "openai", "native")' not in source
    assert "get_harness(_pinned_engine)" in source


def test_every_harness_is_reachable_through_one_seam():
    """The claim itself: no caller names a vendor. `_build_code_agent` resolves a NAME through
    the registry, so adding a provider is a registration, not an edit to the pipeline."""
    import inspect

    from boss_engine.services import code_agent

    source = inspect.getsource(code_agent._build_code_agent)
    assert "build_harness(name, settings)" in source
    assert "if engine ==" not in source          # the if/elif chain this replaced


def test_and_boss_owns_a_harness_of_its_own():
    """So "any provider" does not mean "any provider that ships a coding CLI". An API key gets
    a real multi-turn agent with file and search tools, not one whole-file rewrite."""
    from boss_engine.services.code_agent_tools import tool_schemas

    def _names(read_only: bool) -> set[str]:
        # OpenAI function-tool shape: {"type": "function", "function": {"name": ...}}
        return {t["function"]["name"] for t in tool_schemas(read_only=read_only)}

    assert {"read_file", "glob", "grep", "edit_file", "write_file", "bash"} <= _names(False)

    # And the reviewer's inability to edit is a property of the SCHEMA, not a request in a
    # prompt — the same invariant the CLI harnesses get from `--sandbox read-only`. A tool the
    # model was never told about is one it cannot try to use.
    review = _names(True)
    assert "write_file" not in review and "edit_file" not in review
    assert "read_file" in review
