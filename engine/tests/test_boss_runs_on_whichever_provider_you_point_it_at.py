"""Provider agnosticism, as a property you can check rather than a claim in a README.

Two things were missing. First, pointing BOSS at one provider took SIX settings in THREE
vocabularies — `BOSS_LLM_ENGINE` and `BOSS_CHAT_LLM_ENGINE` speak `claude_cli`, the coding
lanes speak harness names, the strong/low-cost pair speak a third — so an operator who missed
one was indistinguishable from one who set them all. Second, there was no way to check the
result: config says what was ASKED FOR, and only the llm_calls ledger says what HAPPENED.

The distinction that makes the audit honest: routing `anthropic/claude-opus-4.5` through
OpenRouter means the transport is OpenRouter and the weights are Anthropic's. Your prompt
still reached Anthropic. A report that collapsed those would let someone claim "no Anthropic"
while sending Anthropic every token.
"""

from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import provider_audit as pa
from boss_engine.services.harness_registry import available_harnesses, get_harness


# --- the one switch -----------------------------------------------------------

def test_nothing_changes_when_the_lock_is_unset() -> None:
    """An installation that already tuned its per-surface settings must not be silently
    re-pointed by an upgrade."""
    assert Settings().provider_lock == ""
    assert Settings(BOSS_CODE_AGENT_ENGINE="groq").code_agent_engine == "groq"


@pytest.mark.parametrize("lock", ["openrouter", "groq", "deepseek", "ollama", "vllm"])
def test_the_lock_points_every_coding_surface_at_one_provider(lock: str) -> None:
    s = Settings(BOSS_PROVIDER_LOCK=lock)
    assert s.code_agent_engine == lock
    assert s.code_review_engine == lock
    assert s.code_agent_strong_engine == lock
    assert s.code_agent_low_cost_engine == lock


def test_the_lock_also_moves_chat_meetings_and_discovery() -> None:
    """They all resolve through make_chat_adapter, which stays on Claude for the exact
    string 'claude_cli'. A lock that left those behind would be a half-move nobody could see."""
    s = Settings(BOSS_PROVIDER_LOCK="openrouter", OPENROUTER_API_KEY="k")
    assert s.llm_engine == "openrouter" and s.chat_llm_engine == "openrouter"
    assert s.llm_engine != "claude_cli"


def test_locking_to_an_api_lane_binds_the_credentials_chat_actually_reads() -> None:
    """Chat goes through OpenAIAdapter, which reads openai_api_key/openai_base_url. Without
    binding them a 'lock' would leave those surfaces calling whatever OPENAI_* happened to be
    set — the exact silent mis-routing this feature exists to end."""
    s = Settings(BOSS_PROVIDER_LOCK="openrouter", OPENROUTER_API_KEY="or-key")
    assert s.openai_api_key == "or-key"
    assert s.openai_base_url == "https://openrouter.ai/api/v1"


def test_locking_away_from_azure_clears_azure() -> None:
    """OpenAIAdapter dispatches to Azure whenever AZURE_OPENAI_* are present, which would
    beat the lock silently."""
    s = Settings(BOSS_PROVIDER_LOCK="deepseek", DEEPSEEK_API_KEY="d",
                 AZURE_OPENAI_KEY="a", AZURE_OPENAI_ENDPOINT="https://e",
                 AZURE_OPENAI_DEPLOYMENT="dep")
    assert s.azure_openai_key is None and s.azure_openai_endpoint is None


def test_locking_to_claude_puts_chat_back_on_the_claude_cli() -> None:
    s = Settings(BOSS_PROVIDER_LOCK="claude_code")
    assert s.code_agent_engine == "claude_code"
    assert s.llm_engine == "claude_cli" and s.chat_llm_engine == "claude_cli"


# --- transport is not vendor --------------------------------------------------

@pytest.mark.parametrize(
    "model,vendor",
    [("anthropic/claude-opus-4.5", "anthropic"), ("claude-3-haiku", "anthropic"),
     ("gpt-5.2-codex", "openai"), ("openai/gpt-4o", "openai"),
     ("deepseek/deepseek-chat", "deepseek"), ("google/gemini-3.1-pro", "google"),
     ("z-ai/glm-4.5", "zhipu"), ("moonshotai/kimi-k2", "moonshot"),
     ("qwen/qwen3-32b", "alibaba"), ("", "unknown"), ("something-unheard-of", "unknown")],
)
def test_the_vendor_is_read_from_the_model_not_the_endpoint(model: str, vendor: str) -> None:
    assert pa.vendor_of(model) == vendor


def test_claude_over_openrouter_is_still_anthropic() -> None:
    """THE point of the audit. Anything else lets an operator believe they left Anthropic
    while sending Anthropic every token."""
    [bucket] = pa.summarise([
        {"engine": "openrouter", "model": "anthropic/claude-opus-4.5",
         "stage": "review_code", "ok": True, "cost_usd_micros": 2000},
    ])
    assert bucket["transport"] == "openrouter"
    assert bucket["vendor"] == "anthropic"


def test_a_claim_of_no_anthropic_is_refuted_by_the_ledger() -> None:
    summary = pa.summarise([
        {"engine": "openrouter", "model": "deepseek/deepseek-chat", "ok": True,
         "cost_usd_micros": 500},
        {"engine": "openrouter", "model": "anthropic/claude-opus-4.5", "ok": True,
         "cost_usd_micros": 2000},
    ])
    bad = pa.violations(summary, expect="deepseek", dimension="vendor")
    assert [b["vendor"] for b in bad] == ["anthropic"]


def test_a_clean_run_reports_no_violations() -> None:
    summary = pa.summarise([
        {"engine": "openrouter", "model": "deepseek/deepseek-chat", "ok": True,
         "cost_usd_micros": 500},
    ])
    assert pa.violations(summary, expect="deepseek") == []
    assert pa.violations(summary, expect=None) == []


def test_failures_are_counted_separately_from_successes() -> None:
    """A provider that was CALLED and refused still received the prompt."""
    [bucket] = pa.summarise([
        {"engine": "claude_code", "model": "claude-opus-4.5", "ok": False,
         "cost_usd_micros": 0},
    ])
    assert bucket["calls"] == 1 and bucket["failed"] == 1 and bucket["ok"] == 0


# --- CLI harnesses are declared, not coded ------------------------------------

@pytest.mark.parametrize("name", ["codex", "cursor", "aider"])
def test_the_popular_cli_agents_are_registered(name: str) -> None:
    assert get_harness(name).kind == "cli"


def test_an_unproven_harness_says_so() -> None:
    """A spec written from a CLI's documentation is a hypothesis. Presenting it as ready is
    how a registry stops meaning anything."""
    for name in ("codex", "cursor", "aider"):
        assert get_harness(name).verified is False
    assert get_harness("claude_code").verified is True


def test_a_missing_binary_is_a_readable_error_not_a_crash() -> None:
    from boss_engine.services.cli_harness import (
        CliHarnessSpec,
        CliHarnessUnavailable,
        GenericCliCodeAgent,
    )

    agent = GenericCliCodeAgent(
        Settings(), CliHarnessSpec(name="nope", binary="definitely-not-installed-xyz"))
    with pytest.raises(CliHarnessUnavailable, match="not installed"):
        agent.run_role("do a thing", __import__("pathlib").Path("."))


def test_a_cli_with_no_read_only_mode_is_refused_for_a_review() -> None:
    """'The reviewer cannot edit' is an invariant of the governed pipeline. A CLI that
    cannot hold it must be refused for the step, not trusted to behave."""
    from boss_engine.services.cli_harness import (
        BUILTIN_CLI_HARNESSES,
        CliHarnessUnavailable,
        GenericCliCodeAgent,
    )

    agent = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["codex"])
    with pytest.raises(CliHarnessUnavailable, match="read-only"):
        agent.run_role("review this", __import__("pathlib").Path("."), read_only=True)


def test_an_operator_can_correct_the_flags_without_a_release() -> None:
    """These flags are a guess until someone runs the binary."""
    from boss_engine.services.cli_harness import load_specs

    specs = load_specs(Settings(
        BOSS_CLI_HARNESSES_JSON='{"codex": {"args": ["exec", "--json"], '
                                '"read_only_args": ["--read-only"]}}'))
    assert specs["codex"].args == ("exec", "--json")
    assert specs["codex"].supports_read_only()


def test_bad_override_json_does_not_take_the_harnesses_down() -> None:
    from boss_engine.services.cli_harness import load_specs

    specs = load_specs(Settings(BOSS_CLI_HARNESSES_JSON="{not json"))
    assert "codex" in specs and specs["codex"].binary == "codex"


def test_a_cli_harness_never_invents_a_cost() -> None:
    """A subscription CLI does not report per-call spend, and a guessed number would corrupt
    the one place BOSS answers cost questions from."""
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness.GenericCliCodeAgent.run_role)
    assert "cost_usd_micros=0" in src
    assert "cost_unreported" in inspect.getsource(cli_harness.GenericCliCodeAgent)


def test_every_harness_still_declares_what_it_needs() -> None:
    for h in available_harnesses():
        assert h["requires"] and h["description"], h["name"]
