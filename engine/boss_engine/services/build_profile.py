"""Where a build's time and tokens went, per stage — computed from its own transcript.

A 120-minute HelpDesk build was explained by hand, with SQL over `agent_turns` and `llm_calls`:
~85% of implement's time was the model producing tokens (most of them hidden thinking), not
tools running. This module is that analysis, done for every build when it ends, and emitted as
a `code_task.build_profile` event so the next slow build arrives already explained.

Method (per stage):

  * **sessions** — parallel parts write into the same act, interleaved. Each CLI session numbers
    its turns 0, 1, 2… after a `prompt` row at -1, so a row is chained to the session whose last
    index is one less than its own. Model and tool time are then measured per session and summed
    (so parallel sessions can add up to more than the stage's wall time).
  * **model time** — from a tool_result or prompt to the next completion/tool_call in the same
    session: the model reading what came back and producing its next step.
  * **tool time** — tool_call → tool_result; `latency_ms` on the result row wins when recorded.
  * **steps** — one per run of consecutive completion/tool_call rows. The CLI runs a tool while
    the rest of the same message is still streaming, so this is an upper bound on assistant
    messages; `llm_turns` (the CLI's own count, from llm_calls) is the lower one.
  * **duplicates** — a session written twice (live, then again as one batch) is counted once.
  * **think share** — 1 − (Write/Edit + Bash + narration tokens) / output tokens, at
    ~3.8 bytes per token. What is left is tokens nobody reads: hidden thinking.

Never raises from `emit_build_profile`: a diagnostic that can fail a build is worse than none.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

EVENT_TYPE = "code_task.build_profile"
TERMINAL_STATUSES = ("completed", "failed", "blocked", "cancelled")
BYTES_PER_TOKEN = 3.8
#: A gap shorter than this (3 s) is the hand-off between two stages, not waiting.
MIN_GAP_MIN = 0.05

_MODEL_OUT = ("completion", "tool_call")
_MODEL_IN = ("tool_result", "prompt")
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
#: A live "Thinking… 2m 30s so far" beat (code_agent.LivePulse) is BOSS talking, not the model.
_PULSE = re.compile(r"^(Thinking|Writing)… .* so far$")
_TAG = re.compile(r"</?[a-z_]+>")


# ── Loading ───────────────────────────────────────────────────────────────────────────────────

_TURN_COLS = ("stage, act_id, turn_index, direction, tool_name, content, content_bytes, ok, "
              "latency_ms, created_at")


def load_rows(connection: Any, run_id: str) -> dict[str, Any]:
    """The rows a profile is computed from. Read-only."""
    from sqlalchemy import text

    turns = connection.execute(text(
        f"SELECT {_TURN_COLS} FROM agent_turns WHERE code_task_run_id = :r "
        "ORDER BY created_at, turn_index"), {"r": run_id}).mappings().all()
    calls = connection.execute(text(
        "SELECT stage, completion_tokens, cache_read_tokens, cache_write_tokens, turns, "
        "latency_ms, created_at FROM llm_calls WHERE code_task_run_id = :r ORDER BY created_at"),
        {"r": run_id}).mappings().all()
    run = connection.execute(text(
        "SELECT id, project_id, status, phase, created_at, updated_at FROM code_task_runs "
        "WHERE id = :r"), {"r": run_id}).mappings().first()
    return {"turns": [dict(t) for t in turns], "llm_calls": [dict(c) for c in calls],
            "run": dict(run) if run else None}


def compute_profile(run_id: str, connection: Any = None) -> dict[str, Any]:
    """The profile of one run, read from the database (the default engine when no connection)."""
    if connection is None:
        from ..db import begin

        with begin(None) as c:
            rows = load_rows(c, run_id)
    else:
        rows = load_rows(connection, run_id)
    return profile_from_rows(rows["turns"], rows["llm_calls"], rows["run"], run_id=run_id)


# ── Arithmetic ────────────────────────────────────────────────────────────────────────────────

def _ts(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _secs(a: Optional[datetime], b: Optional[datetime]) -> float:
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def _mins(seconds: float) -> float:
    return round(seconds / 60.0, 2)


def _is_pulse(row: Mapping[str, Any]) -> bool:
    return (row.get("direction") == "completion"
            and bool(_PULSE.match(str(row.get("content") or "").strip())))


def _input(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("content")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        # A clipped input is not valid JSON; the leading fields usually still are readable.
        found = re.findall(r'"(file_path|command|pattern|path)"\s*:\s*"((?:[^"\\]|\\.)*)"',
                           str(raw or ""))
        return {k: v for k, v in found}


def _call_label(call: Mapping[str, Any]) -> str:
    args = _input(call)
    detail = (args.get("command") or args.get("file_path") or args.get("pattern")
              or args.get("path") or args.get("description") or "")
    detail = " ".join(str(detail).split())
    return f"{call.get('tool_name') or '?'}: {detail[:100]}" if detail else str(
        call.get("tool_name") or "?")


def _error_line(content: Any) -> str:
    """The line that says what went wrong. "Exit code 1" alone says nothing, so it is joined
    to the first line of output after it."""
    lines = [ln.strip() for ln in _TAG.sub("", str(content or "")).splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    if re.fullmatch(r"Exit code \d+", lines[0]) and len(lines) > 1:
        return f"{lines[0]}: {lines[1]}"[:160]
    return lines[0][:160]


def _sessions(rows: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Split one act's interleaved rows back into the CLI sessions that wrote them."""
    open_: list[dict[str, Any]] = []
    for row in rows:
        k = int(row.get("turn_index") or 0)
        direction = row.get("direction")
        if direction == "prompt" or k < 0:
            open_.append({"last": k, "rows": [row]})
            continue
        exact = [s for s in open_ if s["last"] == k - 1]
        if exact:
            def fit(s: dict[str, Any]) -> tuple[int, float]:
                prev = s["rows"][-1]
                if direction == "tool_result":
                    good = prev.get("direction") == "tool_call" and (
                        prev.get("tool_name") == row.get("tool_name"))
                else:
                    good = prev.get("direction") in _MODEL_IN
                # Then the session that has waited longest.
                t = _ts(prev.get("created_at"))
                return (0 if good else 1, t.timestamp() if t else 0.0)
            target = min(exact, key=fit)
        else:
            behind = [s for s in open_ if s["last"] < k]
            target = max(behind, key=lambda s: s["last"]) if behind and k > 0 else None
            if target is None:
                target = {"last": k, "rows": []}
                open_.append(target)
        target["rows"].append(row)
        target["last"] = k
    return [s["rows"] for s in open_]


def _stage_profile(rows: list[Mapping[str, Any]], calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_act: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_act[row.get("act_id")].append(row)

    kept, duplicate_rows = [], 0
    for act_rows in by_act.values():
        seen: set[tuple] = set()
        for session in _sessions(act_rows):
            sig = tuple((r.get("turn_index"), r.get("direction"), r.get("tool_name"),
                         r.get("content_bytes")) for r in session)
            batch = len(session) > 2 and len({str(r.get("created_at")) for r in session}) == 1
            if batch and sig in seen:
                # The same transcript written twice: once live, then again in one batch at the
                # end (every row of a batch shares its transaction's now()). Counting it twice
                # doubles the stage's reads, bytes and failures and adds no time.
                duplicate_rows += len(session)
                continue
            seen.add(sig)
            kept.append(session)
    rows = [r for session in kept for r in session]

    model_s = tool_s = 0.0
    steps = 0
    sessions = 0
    tools: list[tuple[float, str]] = []
    for session in kept:
        sessions += 1
        since: Optional[datetime] = None     # the last tool_result/prompt not yet answered
        in_step = False
        pending: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in session:
            direction = row.get("direction")
            at = _ts(row.get("created_at"))
            if direction in _MODEL_OUT:
                if since is not None:
                    model_s += _secs(since, at)
                    since = None
                if not in_step:
                    steps += 1
                    in_step = True
                if direction == "tool_call":
                    pending[str(row.get("tool_name") or "")].append(row)
            elif direction in _MODEL_IN:
                since = at
                in_step = False
                if direction == "tool_result":
                    queue = pending.get(str(row.get("tool_name") or ""))
                    call = queue.pop(0) if queue else None
                    recorded = int(row.get("latency_ms") or 0) / 1000.0
                    took = recorded or (_secs(_ts(call.get("created_at")), at) if call else 0.0)
                    tool_s += took
                    if call is not None:
                        tools.append((took, _call_label(call)))

    tool_calls = [r for r in rows if r.get("direction") == "tool_call"]
    written = sum(int(r.get("content_bytes") or 0) for r in tool_calls
                  if r.get("tool_name") in _WRITE_TOOLS)
    bash = sum(int(r.get("content_bytes") or 0) for r in tool_calls if r.get("tool_name") == "Bash")
    narration = sum(int(r.get("content_bytes") or 0) for r in rows
                    if r.get("direction") == "completion")
    output = sum(int(c.get("completion_tokens") or 0) for c in calls)
    written_t, bash_t, narr_t = (round(b / BYTES_PER_TOKEN) for b in (written, bash, narration))
    visible = written_t + bash_t + narr_t
    think_t = max(0, output - visible)

    failed = [r for r in rows if r.get("direction") == "tool_result" and not r.get("ok", True)]
    errors = Counter(f"{r.get('tool_name') or '?'}: {_error_line(r.get('content'))}" for r in failed)

    reads = Counter(_input(r).get("file_path") for r in tool_calls if r.get("tool_name") == "Read")
    reads.pop(None, None)
    reread = sum(n - 1 for n in reads.values() if n > 1)

    stamps = [t for t in (_ts(r.get("created_at")) for r in rows) if t]
    stamps += [t for t in (_ts(c.get("created_at")) for c in calls) if t]
    start, end = (min(stamps), max(stamps)) if stamps else (None, None)

    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "wall_min": _mins(_secs(start, end)),
        "model_min": _mins(model_s),
        "tool_min": _mins(tool_s),
        "sessions": sessions,
        "duplicate_rows": duplicate_rows,
        "steps": steps,
        "llm_turns": sum(int(c.get("turns") or 0) for c in calls),
        "output_tokens": output,
        "cache_read_tokens": sum(int(c.get("cache_read_tokens") or 0) for c in calls),
        "written_bytes": written,
        "written_tokens": written_t,
        "bash_tokens": bash_t,
        "narration_tokens": narr_t,
        "think_tokens": think_t,
        "think_share": round(think_t / output, 3) if output else None,
        "think_tokens_per_step": round(think_t / steps) if steps else None,
        "tool_calls": len(tool_calls),
        "failed_tool_calls": len(failed),
        "top_errors": [{"error": e, "count": n} for e, n in errors.most_common(5)],
        "slowest_tools": [{"tool": label, "seconds": round(s, 1)}
                          for s, label in sorted(tools, key=lambda x: -x[0])[:5]],
        "reads": sum(reads.values()),
        "rereads": reread,
        "files_reread": sum(1 for n in reads.values() if n > 1),
    }


_SUMMED = ("model_min", "tool_min", "sessions", "duplicate_rows", "steps", "llm_turns", "output_tokens",
           "cache_read_tokens", "written_bytes", "written_tokens", "bash_tokens",
           "narration_tokens", "think_tokens", "tool_calls", "failed_tool_calls", "reads",
           "rereads")


def profile_from_rows(turns: Iterable[Mapping[str, Any]], llm_calls: Iterable[Mapping[str, Any]],
                      run: Optional[Mapping[str, Any]] = None, *,
                      run_id: Optional[str] = None) -> dict[str, Any]:
    """The pure part: rows in, profile out. Rows are dicts shaped like the table columns."""
    turns = [t for t in turns if not _is_pulse(t)]
    turns.sort(key=lambda t: (_ts(t.get("created_at")) or datetime.min, int(t.get("turn_index") or 0)))
    calls = list(llm_calls)
    t_by: dict[str, list] = defaultdict(list)
    c_by: dict[str, list] = defaultdict(list)
    for t in turns:
        t_by[str(t.get("stage") or "?")].append(t)
    for c in calls:
        c_by[str(c.get("stage") or "?")].append(c)

    stages = {name: _stage_profile(t_by.get(name, []), c_by.get(name, []))
              for name in set(t_by) | set(c_by)}
    order = sorted(stages, key=lambda n: stages[n]["start"] or "")

    # Serial gaps: wall time in which no stage was active (its first to its last row).
    spans = sorted((_ts(stages[n]["start"]), _ts(stages[n]["end"]), n)
                   for n in order if stages[n]["start"])
    gaps: list[dict[str, Any]] = []
    merged_end, prev = None, None
    for start, end, name in spans:
        if merged_end is not None and start > merged_end:
            gaps.append({"after": prev, "before": name, "minutes": _mins(_secs(merged_end, start))})
        if merged_end is None or end >= merged_end:
            merged_end, prev = end, name
    run_start = _ts((run or {}).get("created_at"))
    run_end = _ts((run or {}).get("updated_at"))
    if spans and run_start and spans[0][0] > run_start:
        gaps.insert(0, {"after": "run start", "before": spans[0][2],
                        "minutes": _mins(_secs(run_start, spans[0][0]))})
    if spans and run_end and merged_end and run_end > merged_end:
        gaps.append({"after": prev, "before": "run end", "minutes": _mins(_secs(merged_end, run_end))})

    gaps = [g for g in gaps if g["minutes"] >= MIN_GAP_MIN]

    totals: dict[str, Any] = {k: round(sum(stages[n][k] for n in order), 2) for k in _SUMMED}
    for k in ("sessions", "duplicate_rows", "steps", "llm_turns", "output_tokens", "cache_read_tokens",
              "written_bytes", "written_tokens", "bash_tokens", "narration_tokens",
              "think_tokens", "tool_calls", "failed_tool_calls", "reads", "rereads"):
        totals[k] = int(totals[k])
    out = totals["output_tokens"]
    totals["think_share"] = round(totals["think_tokens"] / out, 3) if out else None
    first = min((_ts(s["start"]) for s in stages.values() if s["start"]), default=None)
    last = max((_ts(s["end"]) for s in stages.values() if s["end"]), default=None)
    totals["active_wall_min"] = _mins(_secs(first, last))
    totals["wall_min"] = _mins(_secs(run_start, run_end)) if run_start and run_end else totals["active_wall_min"]
    totals["serial_gap_min"] = round(sum(g["minutes"] for g in gaps), 2)

    return {
        "run_id": run_id or (run or {}).get("id"),
        "status": (run or {}).get("status"),
        "phase": (run or {}).get("phase"),
        "turn_rows": len(turns),
        "stage_order": order,
        "stages": stages,
        "totals": totals,
        "serial_gaps": gaps,
    }


# ── Emission ──────────────────────────────────────────────────────────────────────────────────

def emit_build_profile(engine: Any, run_id: str) -> Optional[dict[str, Any]]:
    """Emit the profile of a run that has reached a terminal state. Never raises.

    Called from the build's single finalize path (agent_acts.settle_code_task). A run that
    settles again with nothing new (same status, same transcript) is not profiled twice.
    """
    try:
        from sqlalchemy import text

        from ..db import begin
        from .core import emit_event

        with begin(engine) as c:
            rows = load_rows(c, run_id)
            run = rows["run"]
            if not run or run.get("status") not in TERMINAL_STATUSES:
                return None
            profile = profile_from_rows(rows["turns"], rows["llm_calls"], run, run_id=run_id)
            last = c.execute(text(
                "SELECT data FROM events WHERE entity_id = :r AND event_type = :k "
                "ORDER BY created_at DESC LIMIT 1"), {"r": run_id, "k": EVENT_TYPE}).scalar()
            if isinstance(last, str):
                last = json.loads(last)
            if isinstance(last, Mapping) and last.get("status") == profile["status"] and (
                    last.get("turn_rows") == profile["turn_rows"]):
                return None
            emit_event(c, str(run["project_id"]), EVENT_TYPE, "boss", "code_task_run", run_id,
                       json.loads(json.dumps(profile, default=str)))
        log.info("build profile %s: %s", run_id, profile["totals"])
        return profile
    except Exception:  # noqa: BLE001 — a diagnostic never fails the run it describes
        log.warning("build profile: could not profile %s", run_id, exc_info=True)
        return None


# ── Reading ───────────────────────────────────────────────────────────────────────────────────

def format_profile(profile: Mapping[str, Any]) -> str:
    """A readable table of a profile, for a terminal."""
    stages, totals = profile.get("stages") or {}, profile.get("totals") or {}
    lines = [f"Build profile {profile.get('run_id')}  status={profile.get('status')} "
             f"phase={profile.get('phase')}  wall {totals.get('wall_min')} min "
             f"(active {totals.get('active_wall_min')}, serial gaps {totals.get('serial_gap_min')})",
             ""]
    head = (f"{'stage':<18}{'wall':>7}{'model':>7}{'tools':>7}{'sess':>5}{'steps':>6}{'turns':>6}"
            f"{'out tok':>9}{'written':>9}{'bash':>7}{'narr':>7}{'think%':>7}{'tok/step':>9}"
            f"{'fail':>5}{'reread':>7}")
    lines += [head, "-" * len(head)]

    def row(name: str, s: Mapping[str, Any]) -> str:
        share = s.get("think_share")
        per = s.get("think_tokens_per_step")
        return (f"{name:<18}{s.get('wall_min', 0):>7.1f}{s.get('model_min', 0):>7.1f}"
                f"{s.get('tool_min', 0):>7.1f}{s.get('sessions', 0):>5}{s.get('steps', 0):>6}"
                f"{s.get('llm_turns', 0):>6}"
                f"{s.get('output_tokens', 0):>9,}{s.get('written_tokens', 0):>9,}"
                f"{s.get('bash_tokens', 0):>7,}{s.get('narration_tokens', 0):>7,}"
                f"{(f'{share * 100:.0f}%' if share is not None else '-'):>7}"
                f"{(per if per is not None else '-'):>9}"
                f"{s.get('failed_tool_calls', 0):>5}{s.get('rereads', 0):>7}")

    for name in profile.get("stage_order") or []:
        lines.append(row(name, stages[name]))
    lines += ["-" * len(head), row("TOTAL", {**totals, "think_tokens_per_step": (
        round(totals["think_tokens"] / totals["steps"]) if totals.get("steps") else None)})]
    lines.append("(minutes; model/tool time is summed over parallel sessions; tokens ≈ bytes/3.8)")

    gaps = profile.get("serial_gaps") or []
    if gaps:
        lines += ["", "Serial gaps (no agent turns):"]
        lines += [f"  {g['minutes']:>6.1f} min  {g['after']} -> {g['before']}" for g in gaps]
    for name in profile.get("stage_order") or []:
        s = stages[name]
        if not (s.get("slowest_tools") or s.get("top_errors")):
            continue
        lines += ["", f"[{name}]  {s.get('tool_calls')} tool calls, {s.get('failed_tool_calls')} "
                  f"failed, {s.get('reads')} reads ({s.get('files_reread')} files re-read, "
                  f"{s.get('rereads')} extra reads), written {s.get('written_bytes', 0) / 1024:.0f} KB"
                  + (f", {s['duplicate_rows']} duplicate rows ignored" if s.get("duplicate_rows") else "")]
        for t in s.get("slowest_tools") or []:
            lines.append(f"  slow {t['seconds']:>7.1f}s  {t['tool']}")
        for e in s.get("top_errors") or []:
            lines.append(f"  err  x{e['count']:<3} {e['error']}")
    return "\n".join(lines)
