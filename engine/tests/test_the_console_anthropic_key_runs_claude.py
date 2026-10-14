"""The Anthropic API key stored in the console runs every `claude` BOSS starts (BOSSAPP-530).

The operator wants to disconnect the Claude subscription and run on API credits, set up only in
the console (Repo and models -> Anthropic API -> Store credential). One resolution decides which
credential a `claude` child gets and who pays (claude_credential): the subscription's login, else
the stored key, else ANTHROPIC_API_KEY — or the key first when the operator says so. These pin
the order, where the key goes (the CLI child only), and that a call on it is booked as money.

Database-backed cases (storing through the API, disconnecting, routing) are in
apps/api/tests/test_the_console_anthropic_key_runs_claude_db.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import claude_credential as cc
from boss_engine.services.cli_harness import CredentialStoreUnavailable

TOKEN = "sk-ant-oat01-" + "t" * 48          # shaped like a login; not a real one
KEY = "sk-ant-api03-" + "k" * 48            # shaped like an API key; not a real one
ENV_KEY = "sk-ant-api03-" + "e" * 48


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                 "BOSS_ANTHROPIC_CREDENTIAL_ORDER"):
        monkeypatch.delenv(name, raising=False)


def _store(monkeypatch, **held):
    """The install's secret store, by name: CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY."""
    def lookup(settings, name, *, strict=False, include_env=True):
        value = held.get(name)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential", lookup)


def _settings(**over) -> Settings:
    return Settings().model_copy(update={"claude_code_oauth_token": "",
                                         "anthropic_api_key": None, **over})


# ── the order ──────────────────────────────────────────────────────────────────────────────

def test_subscription_first_by_default_then_the_stored_key_then_the_environment(monkeypatch):
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    got = cc.resolve_claude_credential(_settings())
    assert (got.kind, got.value, got.billing) == ("subscription", TOKEN, "subscription")

    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)                     # no login
    got = cc.resolve_claude_credential(_settings())
    assert (got.kind, got.value, got.billing) == ("stored_key", KEY, "metered")

    _store(monkeypatch)                                            # nothing stored
    got = cc.resolve_claude_credential(_settings())
    assert (got.kind, got.value, got.billing) == ("env_key", ENV_KEY, "metered")

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert cc.resolve_claude_credential(_settings()).kind == "none"


def test_api_key_first_puts_the_stored_key_ahead_of_a_working_login(monkeypatch):
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    got = cc.resolve_claude_credential(_settings(anthropic_credential_order="api_key_first"))
    assert (got.kind, got.value) == ("stored_key", KEY)
    monkeypatch.setenv("BOSS_ANTHROPIC_CREDENTIAL_ORDER", "api_key_first")
    assert Settings().anthropic_credential_order == "api_key_first"
    # Nonsense is not an order: subscription first.
    assert cc.order_of(_settings(anthropic_credential_order="cheapest")) == "subscription_first"


def test_a_store_that_did_not_answer_is_not_read_as_nothing_stored(monkeypatch):
    down = CredentialStoreUnavailable("CLAUDE_CODE_OAUTH_TOKEN", "timeout", transient=True)
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=down, ANTHROPIC_API_KEY=KEY)
    with pytest.raises(CredentialStoreUnavailable):
        cc.resolve_claude_credential(_settings())
    # The install's own key still answers, as it always could.
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    assert cc.resolve_claude_credential(_settings()).kind == "env_key"


def test_a_bound_resolution_is_used_where_there_is_no_connection(monkeypatch):
    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN)             # would win if asked again
    bound = cc.ClaudeCredential("stored_key", KEY).bind(_settings())
    got = cc.resolve_claude_credential(bound)
    assert (got.kind, got.value) == ("stored_key", KEY)
    assert KEY not in repr(got) and KEY not in repr(bound)


# ── where the key goes ─────────────────────────────────────────────────────────────────────

def test_the_key_reaches_the_cli_child_and_nothing_else(monkeypatch, tmp_path):
    from boss_engine.services.agent_shell import _shell_environment
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
    from boss_engine.services.code_agent import ClaudeCodeAgent

    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    agent = ClaudeCodeAgent(_settings())
    env = agent._child_env()
    # The coding agent gets a model-proxy run token, never the key (model_proxy).
    assert env["ANTHROPIC_API_KEY"].startswith("boss_mdl_") and KEY not in env.values()
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert agent._billing == "metered"

    # The chat adapter runs the CLI with no tools at all (`--tools ""`): nothing it starts
    # could read its environment.
    chat = ClaudeCliAdapter(_settings())
    env = chat._child_env()
    assert env["ANTHROPIC_API_KEY"] == KEY and chat._billing == "metered"

    import os
    assert "ANTHROPIC_API_KEY" not in os.environ, "never put into BOSS's own environment"
    # The agent's own shell tool never sees an Anthropic credential, even one in the env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    shell = _shell_environment(tmp_path)
    assert "ANTHROPIC_API_KEY" not in shell and "CLAUDE_CODE_OAUTH_TOKEN" not in shell


def test_a_login_and_an_inherited_key_never_reach_the_child_together(monkeypatch):
    from boss_engine.services.code_agent import ClaudeCodeAgent

    _store(monkeypatch, CLAUDE_CODE_OAUTH_TOKEN=TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    agent = ClaudeCodeAgent(_settings())
    env = agent._child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "ANTHROPIC_API_KEY" not in env
    assert agent._billing == "subscription"


def test_the_key_is_redacted_from_logs_once_resolved(monkeypatch):
    from boss_engine.identity.redaction import redact_text

    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    cc.resolve_claude_credential(_settings())
    assert KEY not in redact_text(f"claude exited 1 with env ANTHROPIC_API_KEY={KEY} set")


def test_a_guest_vm_run_carries_the_key_as_its_api_key(monkeypatch):
    from boss_engine.services.governed_code_task_service import _guest_billed, _guest_engine_fields
    from boss_engine.services.sandbox_contract import build_submit_request

    _store(monkeypatch, ANTHROPIC_API_KEY=KEY)
    fields = _guest_engine_fields(_settings(), "claude_code", None)
    assert fields == {"engine": "claude_code", "anthropic_api_key": KEY}
    body = build_submit_request({**fields, "task_id": "crun_x"}, "p")
    assert body["engine_env"] == {"ANTHROPIC_API_KEY": KEY}
    assert _guest_billed({"prompt_tokens": 1}, fields)["billing"] == "metered"


# ── who pays ───────────────────────────────────────────────────────────────────────────────

def test_a_call_on_the_key_is_booked_as_money(monkeypatch):
    from boss_engine.services.code_agent import ClaudeCodeAgent
    from boss_engine.services.llm_telemetry import engine_call_is_included
    from boss_engine.services.model_routing import ModelChoice, turn_is_included

    data = {"usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.25,
            "modelUsage": {"claude-sonnet-4-5": {}}, "_billing": "metered"}
    usage, cost = ClaudeCodeAgent._usage(data, _settings())
    assert usage["billing"] == "metered" and cost == 250_000
    assert not engine_call_is_included("claude_code", usage)
    assert not turn_is_included(ModelChoice("claude_code", "x"), usage)


def test_every_claude_launch_uses_the_one_resolution():
    """No `claude` launcher keeps its own lookup: the login and a stored API key are both
    provider "anthropic", and a second lookup order is how one became the other."""
    services = Path(__file__).resolve().parents[1] / "boss_engine" / "services"
    for name in ("code_agent.py", "claude_cli_adapter.py", "governed_code_task_service.py"):
        text = (services / name).read_text(encoding="utf-8")
        assert "resolve_claude_credential" in text or "bind_claude_credential" in text, name
        assert 'env_fallback_attr="claude_code_oauth_token"' not in text, name
        assert 'key("claude_code_oauth_token")' not in text, name
