"""Per-turn capture of the conversation between an agent and its model.

`llm_calls` records one row per invocation — "solo, claude-sonnet-5, 45 turns, 516183 ms". That
is the right ledger for spend and the wrong one for watching work happen: the longest phase of
every project, eight to ninety minutes of it, was a single number. An operator asking "what is
it doing right now" had nothing to read.

This module turns the CLI's streaming output into one row per turn — what the agent was told,
what it said, what it decided to run, and what came back — and is deliberately unable to break
the build that produces it:

  * Every entry point swallows its own exceptions. A logging subsystem that can fail a customer
    build is worse than no logging subsystem.
  * Content is redacted and truncated ON THE WAY IN. Prompts carry the customer's source code,
    and this is a table we intend to show people.
  * Rows per invocation are capped, so a runaway agent cannot fill the disk with its own
    transcript.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .redaction import redact

#: How much of a turn is kept. Enough to read what happened; far short of a file dump. A turn
#: longer than this keeps its true size in `content_bytes` and is marked `truncated`, so a
#: clipped turn never reads as a short one.
DEFAULT_MAX_CHARS = 4_000

#: Rows per invocation. `--max-turns` is 90 and each turn can emit several events (a completion
#: plus its tool calls plus their results), so this is generous for real work and still bounds
#: a loop that has gone wrong.
DEFAULT_MAX_TURNS_STORED = 600

DIRECTIONS = ("prompt", "completion", "tool_call", "tool_result")
LEVELS = ("verbose", "info", "warn", "error")

#: Tools whose RESULTS an operator actually reads when a build goes wrong. Everything else is
#: verbose: a directory listing is noise until you are looking for it, and levels exist so the
#: default view is readable.
_NOTABLE_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})

_ERROR_MARKERS = ("error", "failed", "exception", "traceback", "fatal")
#: Tools that only look. Their result is what they found, so its words never set its level.
_READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoWrite",
                              "NotebookRead"})
_WARN_MARKERS = ("warn", "deprecat", "retry", "retrying", "timeout", "skipped")


def max_chars() -> int:
    try:
        return max(200, int(os.environ.get("BOSS_AGENT_TURN_MAX_CHARS") or DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def max_turns_stored() -> int:
    try:
        return max(1, int(os.environ.get("BOSS_AGENT_TURN_MAX_ROWS") or DEFAULT_MAX_TURNS_STORED))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS_STORED


def capture_enabled() -> bool:
    """On unless explicitly disabled.

    The switch exists so an operator can turn capture off without a deploy if it ever proves
    expensive — not because it is experimental.
    """
    raw = (os.environ.get("BOSS_CAPTURE_AGENT_TURNS") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _clip(value: Any) -> tuple[str, int, bool]:
    """Redact, then truncate. Returns (content, original_bytes, truncated).

    Redaction runs FIRST. Truncating first would be a way to leak a secret that happens to sit
    inside the kept window while claiming the row was sanitised.
    """
    if value is None:
        return "", 0, False
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str)
        except Exception:
            value = str(value)
    cleaned, _counts = redact(value)
    size = len(cleaned.encode("utf-8", "replace"))
    limit = max_chars()
    if len(cleaned) <= limit:
        return cleaned, size, False
    return cleaned[:limit], size, True


def classify(direction: str, *, tool_name: str = "", content: str = "", ok: bool = True) -> str:
    """The level a turn is read at.

    Most turns are verbose by nature — this is a transcript, and a transcript shown entirely at
    INFO is a wall nobody reads. The levels earn their keep by making the default view the
    turns that changed something or went wrong.
    """
    if not ok:
        return "error"
    if direction == "tool_result" and tool_name in _READ_ONLY_TOOLS:
        # WHAT A FILE SAYS IS NOT HOW THE READ WENT. A successful Read of code that handles
        # errors ("catch (error)", "throw new Error") was filed as an error by the word search
        # below: a HelpDesk build showed "29 errors", 22 of them reads that had worked.
        return "verbose"
    lowered = (content or "").lower()
    if any(marker in lowered for marker in _ERROR_MARKERS):
        # The TOOL decides whether it failed (`ok`, above). Words in a successful result are a
        # hint worth a look, never a failure: "0 failed" and "error handling" are not errors.
        return "warn"
    if any(marker in lowered for marker in _WARN_MARKERS):
        return "warn"
    if direction == "tool_call" and tool_name in _NOTABLE_TOOLS:
        # "It ran the tests" / "it edited a file" is the story of the build.
        return "info"
    if direction == "prompt":
        # What the agent was ASKED is the anchor for everything after it.
        return "info"
    return "verbose"


class StreamParser:
    """The CLI's stream-json output, consumed ONE LINE AT A TIME.

    The CLI emits a JSON object per turn as the turn happens. The original reader collected the
    whole of stdout with `communicate()` and parsed it after the process exited — asking for a
    stream and then discarding the only property that makes it one. A 9-minute implement was
    therefore a black box for 9 minutes and a complete transcript at the end, which is exactly
    backwards: the transcript is most useful while you are still waiting for it.

    Feeding lines as they arrive changes nothing about what is parsed. `parse_stream` below is
    the same function it always was, now expressed as "feed every line, then read the result",
    so the batch and live paths cannot drift apart.
    """

    def __init__(self, cap: Optional[int] = None,
                 clock: Optional[Callable[[], float]] = time.monotonic):
        self.result: Optional[dict[str, Any]] = None
        self.turns: list[dict[str, Any]] = []
        self._index = 0
        self._cap = max_turns_stored() if cap is None else cap
        # A tool_result block names only the id of the call it answers, so on its own a result
        # row reads "something returned this". Remembering the id->name pairing from the
        # tool_use that preceded it is the difference between "Bash failed" and an anonymous
        # error. It must therefore survive ACROSS lines, which is why it lives on the instance.
        self._tool_names: dict[str, str] = {}
        # When each tool_use was seen, so its result row records how long the tool ran
        # (`latency_ms`). Only meaningful when lines are fed as they arrive; `clock=None` (the
        # after-the-fact batch parse) records nothing rather than a fake near-zero.
        self._clock = clock
        self._tool_started: dict[str, float] = {}
        # The last `rate_limit_event` the CLI emitted: its subscription windows' utilisation
        # and reset times, which the model router's headroom reads (model_router.headroom).
        self.rate_limit: Optional[dict[str, Any]] = None

    def feed(self, line: str) -> list[dict[str, Any]]:
        """Consume one line; return the turns it produced (usually zero or one).

        Never raises. A log line we cannot read is a log line we drop, not a failed build.
        """
        produced: list[dict[str, Any]] = []
        line = (line or "").strip()
        if not line or not line.startswith("{"):
            return produced
        try:
            event = json.loads(line)
        except Exception:
            return produced
        if not isinstance(event, dict):
            return produced

        kind = str(event.get("type") or "")
        if kind == "rate_limit_event":
            self.rate_limit = event
            return produced
        if kind == "result":
            # The terminal event carries exactly what --output-format json would have returned.
            self.result = event
            return produced

        message = event.get("message")
        if kind == "user" and isinstance(message, Mapping):
            for block in _blocks(message):
                if block.get("type") == "tool_result":
                    started = self._tool_started.pop(str(block.get("tool_use_id") or ""), None)
                    produced.append({
                        "direction": "tool_result",
                        "tool_name": self._tool_names.get(
                            str(block.get("tool_use_id") or ""), ""),
                        "content": block.get("content"),
                        "ok": not bool(block.get("is_error")),
                        "latency_ms": (int((self._clock() - started) * 1000)
                                       if started is not None and self._clock else 0),
                        "turn_index": self._index,
                    })
                    self._index += 1
        elif kind == "assistant" and isinstance(message, Mapping):
            model = str(message.get("model") or "")
            usage = message.get("usage") if isinstance(message.get("usage"), Mapping) else {}
            tokens = int((usage or {}).get("output_tokens") or 0)
            for block in _blocks(message):
                btype = block.get("type")
                if btype == "text" and str(block.get("text") or "").strip():
                    produced.append({
                        "direction": "completion",
                        "content": block.get("text"),
                        "model": model,
                        "tokens": tokens,
                        "turn_index": self._index,
                    })
                    self._index += 1
                elif btype == "tool_use":
                    if block.get("id"):
                        self._tool_names[str(block["id"])] = str(block.get("name") or "")
                        if self._clock:
                            self._tool_started[str(block["id"])] = self._clock()
                    produced.append({
                        "direction": "tool_call",
                        "tool_name": str(block.get("name") or ""),
                        "content": block.get("input"),
                        "model": model,
                        "turn_index": self._index,
                    })
                    self._index += 1

        # The cap bounds what is STORED. Turns past it are still handed to a live consumer —
        # dropping them would make a long build go quiet exactly when it is most worth
        # watching — they simply stop accumulating here.
        room = max(0, self._cap - len(self.turns))
        self.turns.extend(produced[:room])
        return produced


def parse_stream(stdout: str) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Split the CLI's stream-json output into (final result, turns).

    Returns ``(None, [])`` for anything that is not a stream, so the caller keeps its existing
    single-object parsing path untouched. This function never raises on malformed input: a log
    line we cannot read is a log line we drop, not a failed build.
    """
    parser = StreamParser(clock=None)
    for line in (stdout or "").splitlines():
        parser.feed(line)
    if parser.result is not None and parser.rate_limit is not None:
        parser.result.setdefault("_rate_limit", parser.rate_limit)
    return parser.result, parser.turns


def _blocks(message: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping):
                yield block
    elif isinstance(content, str) and content.strip():
        yield {"type": "text", "text": content}


def record_turns(
    connection: Connection,
    turns: list[Mapping[str, Any]],
    *,
    project_id: Optional[str],
    code_task_run_id: Optional[str] = None,
    stage: Optional[str] = None,
    role_key: Optional[str] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    prompt: Optional[str] = None,
    act_id: Optional[str] = None,
) -> int:
    """Persist a captured conversation. Returns rows written; never raises.

    `act_id` is the signed work these turns belong to (agent_acts), so an Act card can show what
    the agent actually said while doing it.

    The prompt is stored as turn zero when given, because "what was it asked" is the first
    question anyone has about a transcript and it is not in the stream.
    """
    if not turns and not prompt:
        return 0
    rows: list[dict[str, Any]] = []

    if prompt:
        content, size, clipped = _clip(prompt)
        rows.append({
            "id": f"turn_{uuid.uuid4().hex[:20]}",
            "project_id": project_id, "code_task_run_id": code_task_run_id,
            "stage": stage, "role_key": role_key, "engine": engine, "model": model,
            "act_id": act_id,
            "turn_index": -1,          # sorts before the conversation it started
            "direction": "prompt", "tool_name": None,
            "content": content, "content_bytes": size, "truncated": clipped,
            "tokens": 0, "latency_ms": 0,
            "level": classify("prompt", content=content),
            "ok": True, "error": None,
        })

    from .engine_contract import normalise_turn

    for turn in turns:
        # ONE VOCABULARY AT THE BOUNDARY (engine_contract): every engine's tool names and the
        # argument keys the profile reads are BOSS's here, the one writer of this table, so the
        # profile, the levels, the gates and the trims never see an engine's own spelling.
        turn = normalise_turn(turn)
        direction = str(turn.get("direction") or "")
        if direction not in DIRECTIONS:
            continue
        content, size, clipped = _clip(turn.get("content"))
        tool_name = str(turn.get("tool_name") or "") or None
        ok = bool(turn.get("ok", True))
        rows.append({
            "id": f"turn_{uuid.uuid4().hex[:20]}",
            "project_id": project_id,
            "code_task_run_id": code_task_run_id,
            "stage": stage,
            "role_key": role_key,
            "act_id": act_id,
            "engine": engine,
            "model": str(turn.get("model") or model or "") or None,
            "turn_index": int(turn.get("turn_index") or 0),
            "direction": direction,
            "tool_name": tool_name,
            "content": content,
            "content_bytes": size,
            "truncated": clipped,
            "tokens": int(turn.get("tokens") or 0),
            "latency_ms": int(turn.get("latency_ms") or 0),
            # A turn BOSS wrote itself (a live "thinking…" pulse) says its own level.
            "level": (turn["level"] if turn.get("level") in LEVELS
                      else classify(direction, tool_name=tool_name or "", content=content, ok=ok)),
            "ok": ok,
            "error": None if ok else (content[:500] or "tool reported an error"),
        })

    if not rows:
        return 0
    # Written only when there is an act, so a store without the column records as before.
    signed = (", act_id", ", :act_id") if act_id else ("", "")
    try:
        connection.execute(
            text(
                f"""
                INSERT INTO agent_turns (
                    id, project_id, code_task_run_id, stage, role_key, engine, model,
                    turn_index, direction, tool_name, content, content_bytes, truncated,
                    tokens, latency_ms, level, ok, error{signed[0]}
                ) VALUES (
                    :id, :project_id, :code_task_run_id, :stage, :role_key, :engine, :model,
                    :turn_index, :direction, :tool_name, :content, :content_bytes, :truncated,
                    :tokens, :latency_ms, :level, :ok, :error{signed[1]}
                )
                """
            ),
            rows,
        )
    except Exception:
        # A build must never fail because its transcript could not be written.
        return 0
    return len(rows)
