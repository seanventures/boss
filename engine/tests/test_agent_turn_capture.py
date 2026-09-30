"""The longest phase of a build is no longer a single number.

`llm_calls` recorded one row per invocation — "solo, claude-sonnet-5, 45 turns, 516183 ms".
Eight to ninety minutes of reasoning, file edits and test runs, reduced to a count. The
operator's question during a build is "what is it doing right now", and nothing in the system
could answer it.

These pin the capture path: the CLI's streaming output becomes one row per turn, redacted and
truncated on the way in, and it cannot break the build that produces it.

The event shapes below are taken from real `claude -p --output-format stream-json --verbose`
output, not from the documentation.
"""

from __future__ import annotations

import json

import pytest

from boss_engine.services.agent_turns import (
    DEFAULT_MAX_CHARS,
    capture_enabled,
    classify,
    max_chars,
    parse_stream,
)

# ── Real stream events ─────────────────────────────────────────────────────────

def _line(obj) -> str:
    return json.dumps(obj)


ASSISTANT_TEXT = _line({
    "type": "assistant",
    "message": {"model": "claude-sonnet-5", "usage": {"output_tokens": 12},
                "content": [{"type": "text", "text": "I'll list the files."}]},
})
ASSISTANT_TOOL = _line({
    "type": "assistant",
    "message": {"model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "Glob",
                             "input": {"pattern": "*"}}]},
})
TOOL_RESULT = _line({
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_1",
                             "content": "stream.jsonl"}]},
})
RESULT = _line({"type": "result", "subtype": "success", "num_turns": 2, "result": "done"})
NOISE = _line({"type": "system", "subtype": "init", "session_id": "abc"})


def test_a_stream_becomes_one_row_per_turn():
    """The whole point: four readable rows where the ledger had one number."""
    result, turns = parse_stream("\n".join([NOISE, ASSISTANT_TEXT, ASSISTANT_TOOL,
                                            TOOL_RESULT, RESULT]))
    assert result is not None and result["subtype"] == "success"
    assert [t["direction"] for t in turns] == [
        "completion", "tool_call", "tool_result",
    ]
    assert [t["turn_index"] for t in turns] == [0, 1, 2]


def test_the_result_event_is_what_the_caller_already_expected():
    """`stream-json` ends with the same object `--output-format json` would have returned, which
    is why capture could be added without changing a single caller in the build pipeline."""
    result, _ = parse_stream("\n".join([ASSISTANT_TEXT, RESULT]))
    assert result["num_turns"] == 2
    assert result["result"] == "done"


def test_a_tool_result_knows_which_tool_it_came_from():
    """A tool_result block names only the id of the call it answers.

    Without correlating it back, a failed build shows "something returned this error" — the one
    row an operator most needs to read is the one that says least.
    """
    _, turns = parse_stream("\n".join([ASSISTANT_TOOL, TOOL_RESULT, RESULT]))
    assert [t["tool_name"] for t in turns] == ["Glob", "Glob"]


def test_a_failing_tool_is_recorded_as_failing():
    failed = _line({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_1",
                                 "is_error": True, "content": "command not found"}]},
    })
    _, turns = parse_stream("\n".join([ASSISTANT_TOOL, failed, RESULT]))
    assert turns[-1]["ok"] is False


def test_output_that_is_not_a_stream_is_left_alone():
    """The classic single-object format must keep working. If a CLI version stops streaming,
    builds carry on and only the transcript is lost."""
    result, turns = parse_stream('{"type":"other","result":"hi"}')
    assert result is None and turns == []


def test_a_malformed_line_is_dropped_not_raised():
    """A log line we cannot read is a log line we drop. This code sits inside the build path."""
    result, turns = parse_stream("\n".join([
        "not json at all", "{broken", ASSISTANT_TEXT, RESULT,
    ]))
    assert result is not None
    assert len(turns) == 1


def test_empty_output_returns_nothing_rather_than_guessing():
    assert parse_stream("") == (None, [])
    assert parse_stream("   \n  ") == (None, [])


def test_the_row_count_is_capped(monkeypatch):
    """A runaway agent must not be able to fill the disk with its own transcript."""
    monkeypatch.setenv("BOSS_AGENT_TURN_MAX_ROWS", "3")
    _, turns = parse_stream("\n".join([ASSISTANT_TEXT] * 50 + [RESULT]))
    assert len(turns) == 3


# ── Levels ─────────────────────────────────────────────────────────────────────

def test_a_transcript_is_verbose_by_default():
    """Shown entirely at INFO this is a wall nobody reads. Levels exist so the default view is
    the turns that changed something or went wrong."""
    assert classify("completion", content="I'll look at the tests.") == "verbose"
    assert classify("tool_call", tool_name="Read", content="{}") == "verbose"


def test_the_turns_that_changed_something_are_info():
    assert classify("tool_call", tool_name="Bash", content="npm test") == "info"
    assert classify("tool_call", tool_name="Edit", content="{}") == "info"
    # What the agent was ASKED anchors everything after it.
    assert classify("prompt", content="Build a unit converter") == "info"


def test_a_failing_tool_result_is_an_error():
    """The single most useful line in a failed build."""
    assert classify("tool_result", tool_name="Bash", content="npm ERR! failed") == "error"
    assert classify("tool_result", content="anything", ok=False) == "error"


def test_the_same_words_from_the_model_are_only_a_warning():
    """An agent SAYING "that failed" is narration; a tool RETURNING it is evidence. Treating the
    two the same fills the error view with the agent talking about errors."""
    assert classify("completion", content="the previous step failed, retrying") == "warn"


@pytest.mark.parametrize("body", ["deprecated api", "retrying in 2s", "request timeout"])
def test_soft_signals_are_warnings(body):
    assert classify("completion", content=body) == "warn"


# ── Safety ─────────────────────────────────────────────────────────────────────

def test_capture_is_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv("BOSS_CAPTURE_AGENT_TURNS", raising=False)
    assert capture_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("BOSS_CAPTURE_AGENT_TURNS", off)
        assert capture_enabled() is False, off


def test_the_truncation_limit_cannot_be_set_to_something_useless(monkeypatch):
    monkeypatch.setenv("BOSS_AGENT_TURN_MAX_CHARS", "5")
    assert max_chars() >= 200
    monkeypatch.setenv("BOSS_AGENT_TURN_MAX_CHARS", "not-a-number")
    assert max_chars() == DEFAULT_MAX_CHARS


# ── Capture must never cost a build ────────────────────────────────────────────

def _invoke_source() -> str:
    """The body of `_invoke`, bounded FORWARD from its own definition.

    `run_role` is declared three times in this file — a Protocol, the real agent, and a shim —
    and the first of them sits ABOVE `_invoke`. Slicing between the two by first occurrence
    therefore ran backwards and produced an empty string, which "contains" nothing and passes
    any negative assertion while proving none of them.
    """
    import pathlib

    src = pathlib.Path("engine/boss_engine/services/code_agent.py").read_text(encoding="utf-8")
    start = src.index("    def _invoke(")
    body = src[start:src.index("    def run_role(", start)]
    assert len(body) > 500, "the slice did not capture _invoke"
    return body


def test_the_command_is_rebuilt_per_attempt_so_capture_can_be_dropped():
    """The flags changed on a function that runs for EVERY customer build.

    A transcript is never worth a build. If a CLI version rejects `--output-format stream-json`
    or emits a stream this cannot read, the right outcome is a build that succeeds without a
    transcript — which is only possible if the argv is rebuildable mid-retry.
    """
    invoke = _invoke_source()
    assert "def base_cmd(stream: bool)" in invoke, "argv is fixed before the retry loop"
    # The PROPERTY — argv is rebuilt inside the loop, so dropping capture on a retry changes
    # the flags. The call now also returns the stdin prompt (a long conversation cannot be an
    # argv entry), which is why this matches the call rather than the whole assignment.
    assert "base_cmd(capturing)" in invoke, "the retry reuses the first attempt's flags"


def test_unreadable_output_drops_capture_instead_of_failing():
    invoke = _invoke_source()
    # Exit 0 but unparseable: a capture problem, not a build problem.
    assert "capturing = False" in invoke
    assert "retried without capture" in invoke
    # Non-zero exit with nothing parseable is what "flag not supported" looks like — every
    # other failure mode still prints a result object.
    assert "if capturing and data is None:" in invoke
