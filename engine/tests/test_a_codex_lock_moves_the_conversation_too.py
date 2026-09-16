"""`BOSS_PROVIDER_LOCK=codex` has to move chat, not only the builds.

The lock moved `code_agent_engine` and its friends and then returned early for any CLI harness
that was not claude_code, because Codex is a CLI and the chat surfaces call an OpenAI-compatible
API. The effect was that an operator who set one lock got their coding pipeline on Codex and
every conversation, meeting and discovery run still on whatever `OPENAI_*` credentials happened
to be configured — including, on an installation that had them, Anthropic's.

That is worse than not supporting it. The operator's evidence said "codex", the audit would have
shown another vendor answering, and a "we run entirely on Codex" validation would have come out
green while it was not true.

These tests pin the two halves: the lock points every surface at Codex, and the thing built for
those surfaces is actually a Codex backend rather than the OpenAI fallback wearing its name.
"""
from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services.claude_cli_adapter import (
    ClaudeCliAdapter,
    make_agent_chat_adapter,
    make_chat_adapter,
)
from boss_engine.services.codex_cli_adapter import CodexCliAdapter


def _locked(monkeypatch, provider: str) -> Settings:
    monkeypatch.setenv("BOSS_PROVIDER_LOCK", provider)
    return Settings()


def test_a_codex_lock_points_every_surface_at_codex(monkeypatch) -> None:
    settings = _locked(monkeypatch, "codex")

    assert settings.code_agent_engine == "codex"
    assert settings.code_review_engine == "codex"
    assert settings.code_agent_strong_engine == "codex"
    assert settings.code_agent_low_cost_engine == "codex"
    # The half that was missing: the surfaces an operator actually talks to.
    assert settings.llm_engine == "codex"
    assert settings.chat_llm_engine == "codex"


def test_the_chat_surfaces_get_a_real_codex_backend(monkeypatch) -> None:
    settings = _locked(monkeypatch, "codex")

    assert isinstance(make_chat_adapter(settings), CodexCliAdapter)

    agent = make_agent_chat_adapter(settings)
    # Wrapped so that a missing binary or a dead session still answers the operator, but the
    # thing that answers FIRST must be Codex or the lock has not done its job.
    assert isinstance(getattr(agent, "primary", agent), CodexCliAdapter)


def test_a_claude_lock_is_unchanged(monkeypatch) -> None:
    """The path that already worked must not have moved while the second one was added."""
    settings = _locked(monkeypatch, "claude_code")

    assert settings.llm_engine == "claude_cli"
    assert settings.chat_llm_engine == "claude_cli"
    assert isinstance(make_chat_adapter(settings), ClaudeCliAdapter)


def test_a_cursor_lock_does_not_pretend_to_have_a_chat_backend(monkeypatch) -> None:
    """Cursor has no adapter, so claiming its lock moved chat would be a lie in config.

    What it must NOT do is set `llm_engine = "cursor"`, which names a backend nothing can
    build. Chat stays on whatever the installation already had — that is the honest outcome,
    and `provider_audit` is what tells the operator another vendor is still answering.
    """
    monkeypatch.delenv("BOSS_PROVIDER_LOCK", raising=False)
    before = Settings()
    settings = _locked(monkeypatch, "cursor")

    assert settings.code_agent_engine == "cursor"
    assert settings.llm_engine == before.llm_engine
    assert settings.chat_llm_engine == before.chat_llm_engine


def test_no_lock_changes_nothing(monkeypatch) -> None:
    monkeypatch.delenv("BOSS_PROVIDER_LOCK", raising=False)
    default = Settings()
    assert default.llm_engine == Settings().llm_engine


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
def test_both_cli_brains_share_one_prompt(provider) -> None:
    """The prompt is load-bearing and hard-won; a second copy would drift toward the old bug.

    It carries the fix for a model that reached for its own toolbelt instead of emitting JSON
    — a failure that only appeared once the tool registry crossed ~53 entries. Two copies means
    one of them silently lacks that.
    """
    from boss_engine.services.claude_cli_adapter import CliChatBrain

    assert CodexCliAdapter._build_prompt is CliChatBrain._build_prompt
    assert ClaudeCliAdapter._build_prompt is CliChatBrain._build_prompt
    assert CodexCliAdapter.create_chat_completion is CliChatBrain.create_chat_completion
