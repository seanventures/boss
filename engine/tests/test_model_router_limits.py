"""The limit-message corpus (docs/product/MODEL_ROUTER.md, Decision 18): every phrasing seen
from Claude Code, Codex and OpenAI-compatible APIs, the window it closes and when it reopens.
One parser serves the router's headroom and Fixed mode's provider_limits alike.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from boss_engine.services.model_router.headroom import windows_from_limit_message
from boss_engine.services.provider_limits import classify_limit

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)  # a Tuesday

CORPUS = [
    ("You've hit your weekly limit · resets Mon 12:00am", "seven_day", "2026-10-12T00:00"),
    ("You've hit your weekly limit · resets Mon 12:00am (America/Los_Angeles)", "seven_day",
     "2026-10-12T07:00"),
    ("You've hit your session limit · resets 3:45pm", "five_hour", "2026-10-06T15:45"),
    ("You've hit your session limit · resets 3pm (Europe/London)", "five_hour",
     "2026-10-06T14:00"),
    ("You've hit your Opus limit · resets Oct 13, 9am", "seven_day_opus", "2026-10-13T09:00"),
    # "your limit" with no window named: the session window when it reopens within five hours,
    # the week when it is days away.
    ("You've hit your limit · resets 9am (America/New_York)", "five_hour", "2026-10-06T13:00"),
    ("You've hit your limit · resets Oct 9, 3pm (America/New_York)", "seven_day",
     "2026-10-09T19:00"),
    ("Weekly limit reached ∙ resets Oct 13, 10am", "seven_day", "2026-10-13T10:00"),
    ("5-hour limit reached ∙ resets 3am", "five_hour", "2026-10-07T03:00"),
    ("Session limit reached ∙ resets 7pm", "five_hour", "2026-10-06T19:00"),
    ("Opus weekly limit reached ∙ resets Oct 13, 10am", "seven_day_opus", "2026-10-13T10:00"),
    ("You've hit your weekly limit · resets Oct 13 at 10am (America/Los_Angeles)", "seven_day",
     "2026-10-13T17:00"),
    ("You've hit your weekly limit · resets Oct 13 at 10am (Asia/Kolkata)", "seven_day",
     "2026-10-13T04:30"),
    ("Claude AI usage limit reached|1760000000", "five_hour", "2025-10-09T08:53"),
    ("Claude usage limit reached. Your limit will reset at 8am (America/Chicago).", "five_hour",
     "2026-10-06T13:00"),
    ("You've hit your org's monthly spend limit", "monthly", "2026-11-01T00:00"),
    ("You've hit your usage limit. Upgrade to Pro or try again at Oct 9th, 2026 10:06 PM.",
     "seven_day", "2026-10-09T22:06"),
    ("Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check "
     "your plan'}}", "monthly", "2026-11-01T00:00"),
    ("Rate limit reached for gpt-4o in organization org-x on tokens per min (TPM): Limit "
     "30000. Please try again in 20s.", "minute", "2026-10-06T09:00"),
    ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"This request '
     'would exceed your rate limit"}} retry-after: 30', "minute", "2026-10-06T09:00"),
    ("stream error: exceeded retry limit, last status: 429 Too Many Requests", "minute",
     "2026-10-06T09:01"),
]


@pytest.mark.parametrize("message,window,resets", CORPUS)
def test_every_limit_message_names_its_window_and_reset(message, window, resets):
    got = windows_from_limit_message(message, NOW)
    assert window in got, got
    if resets:
        assert got[window]["resets_at"].startswith(resets), got[window]["resets_at"]


@pytest.mark.parametrize("message", [
    "API Error: 529 Overloaded", "syntax error near line 3", "claude CLI not found",
    "Traceback: KeyError 'user_id'"])
def test_a_failure_that_is_not_a_limit_is_not_one(message):
    assert windows_from_limit_message(message, NOW) == {}


def test_fixed_mode_provider_limits_read_the_same_corpus():
    """Fixed mode changed here, on purpose: provider_limits marks a source exhausted for every
    phrasing above (with its reset), except a single model family's limit (an Opus limit)."""
    for message, window, _resets in CORPUS:
        signal = classify_limit(message)
        if window.startswith("seven_day_"):
            assert signal is None, message
        else:
            assert signal is not None and signal.exhausted, message
