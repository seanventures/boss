"""Turns must land WHILE the stage runs, not after it.

The CLI is invoked with `--output-format stream-json`, which emits one JSON object per turn as
the turn happens. The reader called `communicate()`, which blocks until the process exits and
returns the whole buffer — so BOSS asked for a stream and then threw away the only property
that makes it one.

The cost was the question nobody could answer: "Engineer implementing, 12 minutes, what is it
doing?" The turns existed the entire time, sitting in a pipe nobody was reading. An operator
watching a live build had a black box, and so did BOSS.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from boss_engine.services.agent_turns import StreamParser, parse_stream
from boss_engine.services.code_agent import _drain


def _assistant(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"model": "claude-sonnet-5", "role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


def _tool_call(tid: str, name: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"model": "claude-sonnet-5", "role": "assistant",
                                   "content": [{"type": "tool_use", "id": tid,
                                                "name": name, "input": {"cmd": "ls"}}]}})


def _tool_result(tid: str, body: str) -> str:
    return json.dumps({"type": "user",
                       "message": {"role": "user",
                                   "content": [{"type": "tool_result", "tool_use_id": tid,
                                                "content": body}]}})


# ── The parser, fed one line at a time ─────────────────────────────────────────

def test_a_turn_is_produced_by_the_line_that_completes_it():
    parser = StreamParser()
    assert parser.feed(_assistant("thinking")) != []
    assert parser.feed("not json at all") == []
    assert parser.feed("") == []


def test_tool_names_survive_across_lines():
    """A tool_result names only the id of the call it answers. Fed line by line, the id->name
    pairing has to be remembered between calls or every result becomes anonymous — which is the
    difference between "Bash failed" and "something failed"."""
    parser = StreamParser()
    parser.feed(_tool_call("t1", "Bash"))
    produced = parser.feed(_tool_result("t1", "done"))
    assert produced[0]["tool_name"] == "Bash"


def test_feeding_line_by_line_matches_parsing_the_whole_buffer():
    """The batch path and the live path must not drift. They are now the same code, and this
    is the test that keeps it that way."""
    lines = [_assistant("hello"), _tool_call("t1", "Edit"), _tool_result("t1", "ok"),
             _assistant("done"),
             json.dumps({"type": "result", "result": "finished", "num_turns": 4})]
    batch_result, batch_turns = parse_stream("\n".join(lines))

    parser = StreamParser()
    live: list[dict] = []
    for line in lines:
        live.extend(parser.feed(line))

    assert parser.result == batch_result
    assert live == batch_turns


def test_turn_indexes_are_continuous_when_streamed():
    parser = StreamParser()
    produced: list[dict] = []
    for line in [_assistant("a"), _tool_call("t1", "Bash"), _tool_result("t1", "r")]:
        produced.extend(parser.feed(line))
    assert [t["turn_index"] for t in produced] == [0, 1, 2]


def test_the_cap_bounds_storage_without_silencing_a_live_watcher():
    """Past the cap the parser stops ACCUMULATING, but a live consumer still sees the turns —
    a long build going quiet exactly when it is most worth watching would defeat the point."""
    parser = StreamParser(cap=2)
    seen = []
    for i in range(6):
        seen.extend(parser.feed(_assistant(f"line {i}")))
    assert len(parser.turns) == 2, "the cap did not bound what is stored"
    assert len(seen) == 6, "a live watcher was starved once the cap was reached"


# ── The reader, against a real subprocess ──────────────────────────────────────

def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-u", "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_lines_arrive_before_the_process_exits():
    """THE WHOLE POINT. The child emits two lines, then sleeps. If the reader only delivered
    on exit, nothing would have arrived by the time we look."""
    child = _spawn(
        "import time,sys\n"
        "print('{\"type\":\"a\"}'); print('{\"type\":\"b\"}'); sys.stdout.flush()\n"
        "time.sleep(1.5)\n"
    )
    seen: list[str] = []
    marks: list[float] = []
    began = time.monotonic()

    def _on_line(line: str) -> None:
        seen.append(line.strip())
        marks.append(time.monotonic() - began)

    out, _err = _drain(child, timeout=30, on_line=_on_line)

    assert len(seen) == 2
    assert marks[0] < 1.0, f"the first line waited for exit ({marks[0]:.2f}s)"
    assert out.count("\n") == 2, "the full buffer must still be returned"


def test_stdout_and_stderr_are_drained_concurrently():
    """Reading one pipe to completion while the other fills is the classic deadlock: the child
    blocks writing to a full stderr buffer, we block on a stdout that never advances, and the
    build hangs until its timeout looking exactly like slow work. Well past a pipe buffer."""
    child = _spawn(
        "import sys\n"
        "for i in range(4000):\n"
        "    sys.stderr.write('e'*100 + '\\n')\n"
        "    sys.stdout.write('{\"type\":\"a\"}\\n')\n"
    )
    out, err = _drain(child, timeout=60)
    assert out.count("\n") == 4000
    assert err.count("\n") == 4000


def test_it_still_raises_timeout_expired_like_communicate_did():
    """The caller turns this into "claude code agent timed out after Ns". Changing the
    exception type would silently change how a hung agent is reported."""
    child = _spawn("import time; time.sleep(30)")
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _drain(child, timeout=0.3)
    finally:
        child.kill()


def test_a_consumer_that_raises_cannot_break_the_build():
    """Live tracing is a diagnostic. A diagnostic that can fail a build is worse than none."""
    child = _spawn("print('{\"type\":\"a\"}')")

    def _explode(_line: str) -> None:
        raise RuntimeError("the live sink is broken")

    out, _err = _drain(child, timeout=30, on_line=_explode)
    assert "type" in out


def test_the_exit_status_is_still_available_to_the_caller():
    child = _spawn("import sys; sys.exit(3)")
    _drain(child, timeout=30)
    assert child.returncode == 3


# ── No double-writing ──────────────────────────────────────────────────────────

def test_a_live_transcript_is_not_written_a_second_time_at_the_end():
    """Persisting live and then again from the full buffer would duplicate every row."""
    import inspect

    from boss_engine.services.code_agent import ClaudeCodeAgent

    src = inspect.getsource(ClaudeCodeAgent.run_role)
    assert '_turns_live' in src, "the caller does not know the transcript is already stored"
    assert 'if not data.pop("_turns_live", False):' in src


# ── The context has to cross the thread boundary ───────────────────────────────

def test_the_reader_thread_sees_the_callers_context():
    """The bug my first version shipped, and the reason it looked like it worked.

    `record_agent_turns` resolves project_id / run_id / db_engine from a ContextVar, and a NEW
    THREAD STARTS WITH A FRESH CONTEXT — it does not inherit the creator's. The prompt row is
    written on the main thread before the readers start, so it appeared; every streamed turn was
    written from the reader thread with an empty context. `begin(None)` falls back to a default
    engine rather than raising, so nothing errored and nothing was attributable. Live tracing
    looked wired up and produced exactly one row per stage.

    Observed in production: 6 minutes into an implement, 1 turn.
    """
    import contextvars

    var = contextvars.ContextVar("probe", default={})
    var.set({"run_id": "crun_probe"})
    seen: list[dict] = []

    child = _spawn("print('{\"type\":\"a\"}')")
    _drain(child, timeout=30, on_line=lambda _l: seen.append(var.get()))

    assert seen, "the consumer never ran"
    assert seen[0] == {"run_id": "crun_probe"}, (
        "the reader thread lost the caller's context, so turns would be written unattributed")


def test_the_context_is_copied_on_the_calling_thread():
    """`copy_context()` must be evaluated where the context exists. Calling it inside the
    thread would copy the thread's own empty context and change nothing."""
    import inspect

    from boss_engine.services import code_agent

    src = inspect.getsource(code_agent._drain)
    body = src[:src.index("for t in threads:")]
    assert "contextvars.copy_context()" in body
    assert "target=ctx.run" in body, "the pump does not run inside the copied context"


# ── The fallback must not be cancelled by a write that did not happen ──────────

def test_the_batch_write_is_only_skipped_when_rows_were_really_written():
    """The regression this shipped with, and the worst kind: it LOST data.

    The caller skips the final batch write when the transcript is already stored. That was
    decided from "the live parser saw turns" — which is true even when every write failed. With
    the reader threads running on an empty context, `begin(None)` fell back to a default engine
    rather than raising, so nothing errored, the parser had its turns, the batch write was
    cancelled, and a run whose call reported 48 turns stored 4.

    Before the streaming change all 48 would have been written at the end. A diagnostic feature
    must never be able to destroy the data it is meant to expose.
    """
    import inspect

    from boss_engine.services.code_agent import ClaudeCodeAgent

    src = inspect.getsource(ClaudeCodeAgent._invoke)
    assert 'live_written["rows"] > 0' in src, (
        "the batch write is cancelled on parsed turns rather than on persisted rows")
    assert "live_parser.turns:" not in src


def test_the_sink_counts_rows_returned_by_the_recorder():
    """`record_agent_turns` returns the number of rows it wrote; 0 means it did not."""
    import inspect

    from boss_engine.services.code_agent import ClaudeCodeAgent

    src = inspect.getsource(ClaudeCodeAgent._invoke)
    sink = src[src.index("def _live_sink"):src.index("for attempt in range(3)")]
    assert "record_agent_turns(produced" in sink
    assert 'written["rows"] +=' in sink
