"""Live, tool-level view of what a coding agent is doing RIGHT NOW.

The console and MCP could previously only report which STAGE a run was in. That is far
too coarse: a run can sit in `implement` for 20 minutes and the operator has no way to
tell whether it is patching two files or rebuilding the whole application from scratch —
those look identical from outside and cost wildly different amounts. Every diagnosis in
this program so far has required a human to open the agent's JSONL transcript by hand.

This reads that transcript and turns it into a terminal-style feed: each tool call as a
line, plus a running tally (files touched, lines written, exploration vs edits, turns and
batching). It is READ-ONLY and never interferes with the run.

Deliberately tolerant: a missing or half-written transcript returns an empty feed rather
than raising, because this is an observability path and must never be able to affect the
work it observes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Claude Code writes one JSONL per session under a slugified copy of the sandbox path.
_PROJECTS_ROOT = Path(os.path.expanduser("~/.claude/projects"))

_EXPLORE = {"Read", "Grep", "Glob", "LS"}
_EDIT = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _session_dirs(run_id: str) -> list[Path]:
    """Transcript dirs for a run. The slug uses '-' where the id uses '_'."""
    if not _PROJECTS_ROOT.is_dir():
        return []
    needle = run_id.replace("_", "-")
    return sorted(
        (p for p in _PROJECTS_ROOT.iterdir() if p.is_dir() and needle in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _describe(name: str, payload: dict) -> str:
    """One human line per tool call — what an operator would want to read."""
    if name == "Bash":
        return (payload.get("command") or "")[:120]
    path = payload.get("file_path") or payload.get("path") or ""
    if path:
        return os.path.basename(str(path))
    return (payload.get("pattern") or payload.get("prompt") or "")[:120]


def live_activity(run_id: str, *, limit: int = 40) -> dict[str, Any]:
    """Recent tool calls + a running tally for `run_id`."""
    feed: list[dict[str, Any]] = []
    files: dict[str, int] = {}
    counts = {"explore": 0, "edit": 0, "bash": 0, "other": 0}
    turns = 0
    tool_calls = 0

    for d in _session_dirs(run_id):
        for jf in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            try:
                lines = jf.read_text(errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001 — a partially flushed line is normal
                    continue
                msg = rec.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                turns += 1
                for block in msg.get("content") or []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    name = block.get("name") or "?"
                    payload = block.get("input") or {}
                    tool_calls += 1
                    if name in _EXPLORE:
                        counts["explore"] += 1
                    elif name in _EDIT:
                        counts["edit"] += 1
                        body = payload.get("content") or payload.get("new_string") or ""
                        base = os.path.basename(str(payload.get("file_path") or ""))
                        if base:
                            files[base] = files.get(base, 0) + len(str(body).splitlines())
                    elif name == "Bash":
                        counts["bash"] += 1
                    else:
                        counts["other"] += 1
                    feed.append({
                        "at": rec.get("timestamp"),
                        "tool": name,
                        "detail": _describe(name, payload),
                    })

    return {
        "run_id": run_id,
        "feed": feed[-limit:],
        "totals": {
            "turns": turns,
            "tool_calls": tool_calls,
            # 1.00 means fully serialized -- every call its own model round trip.
            "calls_per_turn": round(tool_calls / turns, 2) if turns else None,
            **counts,
            "files_touched": len(files),
            "lines_written": sum(files.values()),
        },
        # Sorted biggest-first: the honest signal for "is this a patch or a rebuild?"
        "files": [{"file": f, "lines": n}
                  for f, n in sorted(files.items(), key=lambda x: -x[1])[:15]],
    }


def live_activity_text(run_id: str, *, limit: int = 40) -> str:
    """The same feed rendered as terminal lines, for a console pane or a chat client."""
    data = live_activity(run_id, limit=limit)
    t = data["totals"]
    if not data["feed"]:
        return f"{run_id}: no agent activity recorded yet."
    out = [f"{ln['tool']:<10} {ln['detail']}" for ln in data["feed"]]
    out.append("")
    out.append(f"turns={t['turns']}  calls={t['tool_calls']}  per_turn={t['calls_per_turn']}  "
               f"explore={t['explore']} edit={t['edit']} bash={t['bash']}")
    out.append(f"files={t['files_touched']}  lines_written={t['lines_written']}")
    if data["files"]:
        out.append("top: " + ", ".join(f"{f['file']}({f['lines']})" for f in data["files"][:5]))
    return "\n".join(out)
