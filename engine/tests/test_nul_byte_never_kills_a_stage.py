"""A binary byte in a prompt must not silently cancel a whole stage.

Measured on a live run. The agent built a SQLite-backed app; a binary blob reached the review
prompt through the diff; and all THREE reviewers — security, consistency, adversarial — failed
in 2ms each with "embedded null byte". `subprocess.Popen` rejects the entire command before the
process is started if any argv entry contains a NUL.

The review panel did not run. Nothing said so. The only symptom was a `review_panel` phase that
took 20 seconds instead of minutes, and gates coming back advisory-failing for reasons no
reviewer had actually established. A quality gate that is skipped by an encoding artefact is
worse than one that fails, because it looks like it ran.
"""

from __future__ import annotations

import subprocess

import pytest

from boss_engine.services.code_agent import _argv_safe


def test_a_prompt_with_a_nul_can_actually_be_passed_to_a_process():
    """The end-to-end property, asserted against the real constraint rather than a mock:
    argv with a NUL raises before anything runs."""
    poisoned = "review this diff\x00binary"

    with pytest.raises(ValueError, match="null byte"):
        subprocess.run(["echo", poisoned], capture_output=True)

    # Cleaned, the same prompt is a perfectly ordinary argument.
    done = subprocess.run(["echo", _argv_safe(poisoned)], capture_output=True, text=True)
    assert done.returncode == 0
    assert "review this diff" in done.stdout


def test_the_readable_text_around_the_byte_survives():
    """Dropping the byte, not the prompt. A review on slightly-cleaned text beats no review."""
    cleaned = _argv_safe("SECURITY REVIEW\x00\n- check auth\x00\n- check input")
    assert "\x00" not in cleaned
    assert "SECURITY REVIEW" in cleaned
    assert "- check auth" in cleaned
    assert "- check input" in cleaned


def test_an_ordinary_prompt_is_returned_untouched():
    prompt = "Implement the rota tool.\n\nRun `npm test`."
    assert _argv_safe(prompt) is prompt


@pytest.mark.parametrize("value", ["", "plain text"])
def test_it_is_safe_on_trivial_input(value):
    assert _argv_safe(value) == value


def test_it_says_so_when_it_strips(caplog):
    """Silently cleaning would hide that binary content is leaking into prompts, which is the
    upstream bug this only makes survivable."""
    import logging

    with caplog.at_level(logging.WARNING):
        _argv_safe("a\x00b\x00c")
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "NUL" in said
    assert "2" in said, "the count of stripped bytes is not reported"


def test_every_stage_goes_through_the_same_choke_point():
    """Build AND review dispatch through `_invoke`, so cleaning there covers all of them.
    Sanitising in individual prompt builders would leave whichever one was forgotten."""
    import inspect

    from boss_engine.services.code_agent import ClaudeCodeAgent

    src = inspect.getsource(ClaudeCodeAgent._invoke)
    assert "_argv_safe(prompt)" in src
