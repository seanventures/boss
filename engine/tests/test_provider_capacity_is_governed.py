"""A provider running out of capacity is a fact BOSS records, not an incident to reconstruct.

A codex subscription hit its weekly cap mid-run. Every role touching it failed in ~2 seconds,
the panel stalled 15 minutes, the build filled with red crosses — and the sentence explaining
all of it reached no screen:

    You've hit your usage limit ... try again at Sep 22nd, 2026 10:06 PM.

It was in a container log, and truncated out of the step's error by a slice that kept the CLI's
startup banner instead. The operator spent a day on symptoms and said, correctly: "I never saw
that error at all — I could have switched to a different model."
"""

from __future__ import annotations

from datetime import datetime, timezone

from boss_engine.services.provider_limits import (
    LimitSignal, classify_limit, is_available, source_ids_for_engine,
)

# The message exactly as codex 0.155.1 emitted it.
CODEX = ("ERROR: You've hit your usage limit. Upgrade to Pro "
         "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
         "to purchase more credits or try again at Sep 22nd, 2026 10:06 PM.")


def test_it_recognises_the_message_that_cost_a_day():
    signal = classify_limit(CODEX)
    assert signal is not None and signal.exhausted


def test_it_extracts_when_capacity_returns():
    """The most useful field in the whole message: the answer to 'when can I run again'."""
    assert classify_limit(CODEX).retry_at == datetime(2026, 9, 22, 22, 6, tzinfo=timezone.utc)


def test_it_keeps_the_providers_own_words():
    """Not a paraphrase: the sentence names the plan, the cap and the remedy, and an operator
    deciding whether to wait or switch needs all three."""
    assert "usage limit" in classify_limit(CODEX).detail


def test_other_providers_phrasings_are_recognised():
    for message in (
        "429 Too Many Requests",
        "Your credit balance is too low to access the Anthropic API",
        "You exceeded your current quota, please check your plan and billing details",
        "error: insufficient_quota",
        "rate limit exceeded, retry-after: 900",
    ):
        assert classify_limit(message) is not None, message


def test_a_retry_after_in_seconds_is_understood():
    assert classify_limit("rate limit exceeded, retry-after: 900").retry_after_seconds == 900


def test_a_failure_that_is_not_about_capacity_is_left_alone():
    """Offering 'wait, or switch model' for an unrelated crash is worse than saying nothing."""
    for message in (
        "claude CLI exited 1: command not found",
        "Could not resolve entry module \"index.html\"",
        "codex CLI exited 1: Not inside a trusted directory",
        "", None,
    ):
        assert classify_limit(message) is None, message


def test_availability_is_false_until_the_retry_time_passes():
    capacity = classify_limit(CODEX).as_capacity()
    assert is_available(capacity, now=datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)) is False
    assert is_available(capacity, now=datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)) is True


def test_an_exhaustion_with_no_stated_retry_time_is_not_permanent():
    """It clears when a call succeeds — which is the only evidence that arrives on its own."""
    capacity = classify_limit("429 Too Many Requests").as_capacity()
    assert is_available(capacity) is False
    assert is_available({"exhausted": False, "recovered_at": "2026-09-20T02:00:00+00:00"}) is True


def test_an_engine_with_no_recorded_capacity_is_available():
    for capacity in (None, {}, {"credential_present": True}):
        assert is_available(capacity) is True


def test_each_engine_resolves_to_the_source_that_pays_for_it():
    assert "openai_codex_chatgpt" in source_ids_for_engine(None, "codex")
    assert "anthropic_claude_code" in source_ids_for_engine(None, "claude_code")
    assert "setup-openrouter" in source_ids_for_engine(None, "openrouter")


def test_the_stored_shape_carries_what_an_operator_needs():
    stored = classify_limit(CODEX).as_capacity()
    assert stored["exhausted"] is True
    assert stored["retry_at"].startswith("2026-09-22T22:06")
    assert "usage limit" in stored["detail"]
    assert "observed_at" in stored
