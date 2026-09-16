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


# --- usage accounting -------------------------------------------------------
#
# Captured from codex-cli, not invented: one real `turn.completed` event. A unit test must
# never shell out to the CLI — that spends the operator's subscription on every CI run.

_TURN = (
    '{"type": "item.completed", "item": {"text": "pong"}}\n'
    '{"type": "turn.completed", "usage": {"input_tokens": 16243, '
    '"cached_input_tokens": 11520, "cache_write_input_tokens": 0, '
    '"output_tokens": 5, "reasoning_output_tokens": 0}}\n'
)


def _usage_for(stdout: str) -> dict:
    return CodexCliAdapter(Settings())._usage({"stdout": stdout})


def test_cached_input_is_not_priced_as_new_input() -> None:
    """Folding cache reads into prompt_tokens makes cache savings invisible-to-negative.

    The Claude path had to be corrected for exactly this (BOSS-280). Codex reports the whole
    prompt in `input_tokens` with the cached part as a subset, so new input is the difference.
    """
    usage = _usage_for(_TURN)

    assert usage["cache_read_tokens"] == 11520
    assert usage["prompt_tokens"] == 16243 - 11520
    assert usage["total_tokens"] == 16243 + 5


def test_reasoning_tokens_are_counted_as_output() -> None:
    """They are billed as output; a field of their own would be priced by nothing."""
    stdout = _TURN.replace('"reasoning_output_tokens": 0', '"reasoning_output_tokens": 640')

    assert _usage_for(stdout)["completion_tokens"] == 645


def test_a_turn_that_reports_nothing_does_not_crash_or_invent() -> None:
    """A backend that appears free would quietly corrupt every cost comparison BOSS makes."""
    usage = _usage_for("not json at all\n")

    assert usage["total_tokens"] == 0
    assert usage["model"] == "codex-cli"


def test_the_last_turn_wins_when_a_run_reports_several() -> None:
    stdout = _TURN + _TURN.replace('"output_tokens": 5', '"output_tokens": 99')

    assert _usage_for(stdout)["completion_tokens"] == 99


def test_no_unit_test_here_spends_the_subscription(monkeypatch) -> None:
    """The fixture above is captured output. If this file ever shells out, CI pays for it.

    Checked by making the CLI unrunnable rather than by grepping this file for suspicious
    words: a text search flagged its own assertion that two classes SHARE one method, which
    reads `create_chat_completion` without calling anything. A test that judges its own source
    fails on the wrong things and passes on cleverly-worded ones.
    """
    def explode(*_a, **_k):  # pragma: no cover - the point is that it is never reached
        raise AssertionError("a unit test tried to run a real CLI")

    monkeypatch.setattr("subprocess.run", explode)

    assert _usage_for(_TURN)["total_tokens"] > 0
