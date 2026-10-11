"""A build's page below the strip: what it is doing now, what each tool call did, and the
text an operator searches when it went wrong.

Everything here is read from what BOSS already recorded — the run row, its retry history, its
events, its gate rows and the agent turns it captured. Nothing is estimated: a count of tests is
said only when the test runner printed one, a failing test is named only when the runner named
it, and a time limit is the configured one.

Three readers:

* ``kept_stations`` — a station a retry carried from a saved workspace (or an inherited plan)
  is KEPT, not skipped. The strip says ``Kept from #N``.
* ``now_summary`` — the cockpit's ``summary``: stage and time against its limit, attempt, where
  the attempt was seeded from, the retry note, files so far, the last test result, gates, so the
  page can say what the build is doing without opening anything.
* ``tool_log`` — GET /v1/code-tasks/{id}/tool-log: every tool call of the build in one line
  (the command, the path, how long, the exit code) with its output clipped and redacted, plus
  the verify output, for the page's search.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .redaction import redact_secrets

#: The tools whose argument is a path.
FILE_TOOLS = frozenset({"read", "edit", "write", "multiedit", "notebookedit", "str_replace",
                        "create", "view", "str_replace_editor", "apply_patch", "glob", "grep",
                        "ls"})
#: The tools that change a file: what "files changed so far" counts while a build runs.
WRITE_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit", "str_replace", "create",
                         "apply_patch"})
SHELL_TOOLS = frozenset({"bash", "shell", "run", "exec", "terminal"})

#: How much of a tool call the page carries. A command line is a line; the output is enough to
#: search and to read an error, never a file dump (the stored turn is already ≤ 4 000 chars).
ARG_CHARS = 240
OUTPUT_CHARS = 600
ERROR_CHARS = 2_000
TOOL_LOG_CAP = 800
VERIFY_LINES = 400
FAILING_CAP = 12


def _j(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


# ── kept, not skipped ─────────────────────────────────────────────────────────────────────────

def attempt_of(fctx: Mapping[str, Any], run_id: str) -> Optional[int]:
    """The attempt number of an earlier run of this lineage, from this run's own record: its
    parent is `attempt - 1`; any other ancestor is named in the retry history."""
    if not run_id:
        return None
    mine = fctx.get("attempt")
    if run_id == str(fctx.get("parent_run_id") or "") and isinstance(mine, int) and mine > 1:
        return mine - 1
    for entry in fctx.get("feedback_history") or []:
        if isinstance(entry, dict) and str(entry.get("parent_run_id") or "") == run_id:
            n = entry.get("attempt")
            if isinstance(n, int) and n > 1:
                return n - 1
    return None


def kept_stations(fctx: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The stations this attempt carried from an earlier one instead of re-running.

    A retry seeded from a saved workspace (`seed_workspace_from`) skips the design chain and
    write_tests: the plan and the tests on its branch are the earlier attempt's. A retry that
    inherited a plan keeps the Plan; one resumed after a harness failure keeps what that attempt
    finished. Said only for a station the run then passed without an act (stations_for)."""
    out: dict[str, dict[str, Any]] = {}
    seed = str(fctx.get("seed_workspace_from") or "").strip()
    parent = str(fctx.get("parent_run_id") or "").strip()
    if seed:
        n = attempt_of(fctx, seed)
        for key in ("plan", "tests"):
            out[key] = {"from_run": seed, "attempt": n,
                        "tip": f"Carried from {('#' + str(n)) if n else 'an earlier attempt'}'s "
                               "saved workspace, not re-run"}
        return out
    if parent and isinstance(fctx.get("inherited_design"), dict) \
            and (fctx["inherited_design"].get("plan") or "").strip():
        n = attempt_of(fctx, parent)
        out["plan"] = {"from_run": parent, "attempt": n,
                       "tip": f"The plan from {('#' + str(n)) if n else 'the last attempt'}, "
                              "not re-planned"}
    done = {str(s) for s in fctx.get("resume_completed_stages") or []}
    if parent and "write_tests" in done:
        n = attempt_of(fctx, parent)
        out["tests"] = {"from_run": parent, "attempt": n,
                        "tip": f"The tests {('#' + str(n)) if n else 'the last attempt'} wrote, "
                               "not re-written"}
    return out


def kept_word(kept: Mapping[str, Any]) -> str:
    n = kept.get("attempt")
    return f"Kept from #{n}" if n else "Kept"


# ── what a test run printed ──────────────────────────────────────────────────────────────────

_COUNT = re.compile(r"(\d+)\s+(failed|passed|failing|passing|errors?|skipped|pending|total)\b",
                    re.I)
_TAP = re.compile(r"^#\s*(pass|fail|tests)\s+(\d+)\s*$", re.I | re.M)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
_UNITTEST_FAILED = re.compile(r"^FAILED \((.*)\)\s*$", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DURATION = re.compile(r"\s*\(\s*\d+(?:\.\d+)?\s*m?s\s*\)\s*$")
_FAILING = (
    re.compile(r"^FAILED\s+(\S+::\S+?)(?:\s+-\s.*)?$"),               # pytest short summary
    re.compile(r"^(?:FAIL|ERROR):\s+(\S+\s+\(.+\))"),                  # unittest
    re.compile(r"^\s*---\s+FAIL:\s+(\S+)"),                            # go test
    re.compile(r"^not ok \d+\s+-?\s*(.+?)\s*(?:#.*)?$"),               # TAP / node:test
    re.compile(r"^\s*(?:×|✕|✘|✗)\s+(.+)$"),                            # vitest / jest
    re.compile(r"^\s*FAIL\s+(\S+\s+>\s+.+)$"),                         # vitest file > name
)


def verify_counts(output: str) -> dict[str, Any]:
    """Passed / failed / total as the runner printed them, and the failing tests it named.

    Every number is read from a summary line the runner wrote (pytest's `3 failed, 8 passed`,
    jest's `Tests: 7 failed, 4 passed, 11 total`, vitest's `Tests  7 failed | 4 passed (11)`,
    TAP's `# pass 4`, unittest's `Ran 11 tests … FAILED (failures=7)`). None when it wrote none."""
    clean = _ANSI.sub("", output or "")
    passed = failed = total = None
    tap = {k.lower(): int(v) for k, v in _TAP.findall(clean)}
    if tap:
        passed, failed = tap.get("pass"), tap.get("fail")
        total = tap.get("tests")
    ran = _UNITTEST_RAN.findall(clean)
    if passed is None and failed is None and ran:
        total = int(ran[-1])
        bad = _UNITTEST_FAILED.findall(clean)
        failed = (sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", bad[-1]))
                  if bad else 0)
        passed = total - failed
    if passed is None and failed is None:
        for line in reversed(clean.splitlines()):
            found = _COUNT.findall(line)
            kinds = {k.lower() for _, k in found}
            if not found or not kinds & {"failed", "passed", "failing", "passing"}:
                continue
            got: dict[str, int] = {}
            for n, k in found:
                k = k.lower()
                k = {"failing": "failed", "passing": "passed", "error": "errors"}.get(k, k)
                got.setdefault(k, int(n))
            passed = got.get("passed", 0)
            failed = got.get("failed", 0) + got.get("errors", 0)
            total = got.get("total")
            break
    if total is None and (passed is not None or failed is not None):
        total = (passed or 0) + (failed or 0)
    failing: list[str] = []
    for line in clean.splitlines():
        for pattern in _FAILING:
            m = pattern.match(line)
            if m:
                name = _DURATION.sub("", m.group(1).strip())
                if name and name not in failing:
                    failing.append(name[:200])
                break
        if len(failing) >= FAILING_CAP:
            break
    return {"passed": passed, "failed": failed, "total": total, "failing": failing}


# ── the Now summary ──────────────────────────────────────────────────────────────────────────

_SEED_EVENTS = ("code_task.workspace_seeded", "code_task.workspace_seed_missing",
                "code_task.tests_resumed", "code_task.design_resumed")


def _events(connection: Any, project_id: str, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT event_type, data, created_at FROM events WHERE project_id = :p "
             "AND entity_id = :r AND event_type = ANY(:t) ORDER BY created_at"),
        {"p": project_id, "r": run_id, "t": list(_SEED_EVENTS)},
    ).mappings().all()
    return [{"type": r["event_type"], "data": _j(r["data"], {}) or {}, "at": r["created_at"]}
            for r in rows]


def _ancestors(connection: Any, project_id: str, run: Mapping[str, Any],
               hops: int = 12) -> list[dict[str, Any]]:
    """This run, then its parent, its parent's parent … (the attempts before it)."""
    from .code_task_service import load_code_task_run

    out = [dict(run)]
    parent = (_j(run.get("finalize_ctx"), {}) or {}).get("parent_run_id")
    for _ in range(hops):
        if not parent:
            break
        older = load_code_task_run(connection, str(parent))
        if not older or older.get("project_id") != project_id:
            break
        out.append(dict(older))
        parent = (_j(older.get("finalize_ctx"), {}) or {}).get("parent_run_id")
    return out


def _attempt_n(run: Mapping[str, Any]) -> int:
    return int((_j(run.get("finalize_ctx"), {}) or {}).get("attempt") or 1)


def _gate_row(connection: Any, run_id: str) -> Optional[Mapping[str, Any]]:
    return connection.execute(
        text("SELECT gates_passed, gates_failed, created_at FROM quality_gates "
             "WHERE code_task_run_id = :r ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id},
    ).mappings().first()


def _files_so_far(connection: Any, run_id: str, act_ids: list[str]) -> list[str]:
    """The paths the agents wrote in THIS attempt, from their Write/Edit calls, while the run's
    own file list is not written yet (it is, at the end)."""
    rows = connection.execute(
        text("SELECT tool_name, content FROM agent_turns WHERE direction = 'tool_call' "
             "AND (code_task_run_id = :r OR act_id = ANY(:a)) ORDER BY created_at LIMIT 2000"),
        {"r": run_id, "a": act_ids or [""]},
    ).mappings().all()
    seen: list[str] = []
    for r in rows:
        if str(r["tool_name"] or "").strip().lower() not in WRITE_TOOLS:
            continue
        path = call_arg(r["tool_name"], r["content"])
        if path and path not in seen:
            seen.append(path)
    return seen[:200]


def now_summary(connection: Any, settings: Any, project_id: str, run: Mapping[str, Any], *,
                acts: list[Mapping[str, Any]], gate_labels: Any) -> dict[str, Any]:
    """The cockpit's `summary` (see the module doc). `gate_labels` maps a gate name to words."""
    from . import agent_acts

    rid = str(run["id"])
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    ev = _j(run.get("state"), {}) or {}
    stages = [a for a in acts if a.get("kind") == "stage" and a.get("step_id")]
    open_act = next((a for a in reversed(stages) if a.get("ended_at") is None), None)
    stage = None
    if open_act is not None:
        step = str(open_act.get("step_id") or "")
        stage = {"step": step, "title": agent_acts.STAGE_TITLES.get(step, step.replace("_", " ")
                                                                    .capitalize()),
                 "started_at": _iso(open_act.get("started_at")),
                 "role_key": open_act.get("role_key"),
                 "act_id": open_act.get("act_id")}
    limit_s = int(getattr(settings, "code_agent_timeout_seconds", 0) or 0) or None

    # Where this attempt started from.
    events = _events(connection, project_id, rid)
    seeded: Optional[dict[str, Any]] = None
    seed_from = str(fctx.get("seed_workspace_from") or "").strip()
    for e in events:
        d = e["data"]
        if e["type"] == "code_task.workspace_seeded":
            src = str(d.get("from_run") or seed_from)
            seeded = {"from_run": src, "attempt": attempt_of(fctx, src),
                      "files": int(d.get("files") or 0), "tests": None, "at": _iso(e["at"]),
                      "missing": False}
        elif e["type"] == "code_task.workspace_seed_missing":
            src = str(d.get("from_run") or seed_from)
            seeded = {"from_run": src, "attempt": attempt_of(fctx, src), "files": 0,
                      "tests": None, "at": _iso(e["at"]), "missing": True,
                      "why": str(d.get("why") or "")[:200] or None}
    for e in events:
        if e["type"] == "code_task.tests_resumed" and seeded is not None:
            n = e["data"].get("existing_tests")
            seeded["tests"] = int(n) if isinstance(n, int) else None
    if seeded is None and seed_from:
        # Asked to continue from a saved workspace; the sandbox is not seeded yet.
        seeded = {"from_run": seed_from, "attempt": attempt_of(fctx, seed_from), "files": None,
                  "tests": None, "at": None, "missing": False, "pending": True}

    # The person's note on the retry that made this attempt.
    note = None
    parent = str(fctx.get("parent_run_id") or "")
    for entry in reversed(fctx.get("feedback_history") or []):
        if isinstance(entry, dict) and str(entry.get("parent_run_id") or "") == parent:
            note = str(entry.get("fix_note") or "").strip()[:600] or None
            break

    # Files: the run's own list once written, else what its agents wrote so far.
    own = [str(f) for f in list(ev.get("impl_files") or []) + list(ev.get("test_files") or [])
           if f]
    files = own or _files_so_far(connection, rid, [str(a["act_id"]) for a in acts
                                                   if a.get("act_id")])

    # The last test result and the last gate verdict: this attempt's, else the nearest
    # earlier attempt's, said with whose it is.
    verify = None
    gates = None
    chain = _ancestors(connection, project_id, run)
    for r in chain:
        rev = _j(r.get("state"), {}) or {}
        output = str(rev.get("last_verify_output") or "")
        commands = [t for t in rev.get("tests") or [] if isinstance(t, dict)]
        if verify is None and (output.strip() or commands):
            counts = verify_counts(output)
            ok = all(bool(t.get("ok", t.get("exit_code") == 0)) for t in commands) \
                if commands else (counts["failed"] == 0 if counts["failed"] is not None else None)
            verify = {"run_id": str(r["id"]), "attempt": _attempt_n(r), "own": r["id"] == rid,
                      "at": _iso(r.get("updated_at")), "ok": ok, **counts}
        if gates is None:
            g = _gate_row(connection, str(r["id"]))
            if g is not None:
                passed = [str(x) for x in _j(g["gates_passed"], []) or []]
                failed = [str(x) for x in _j(g["gates_failed"], []) or []]
                gates = {"run_id": str(r["id"]), "attempt": _attempt_n(r), "own": r["id"] == rid,
                         "at": _iso(g.get("created_at")),
                         "passed": [gate_labels(x) for x in passed],
                         "failed": [gate_labels(x) for x in failed]}
        if verify is not None and gates is not None:
            break

    return {
        "phase": str(run.get("phase") or "") or None,
        "stage": stage,
        "time_limit_s": limit_s,
        "attempt": _attempt_n(run),
        "seeded": seeded,
        "note": note,
        "files": files[:50],
        "files_count": len(files),
        "files_final": bool(own),
        "verify": verify,
        "gates": gates,
    }


def acts_of(connection: Any, project_id: str, run_id: str) -> list[dict[str, Any]]:
    """A build's stage acts (what its tool calls are filed under)."""
    return [dict(r) for r in connection.execute(
        text("SELECT act_id, kind, step_id, started_at, ended_at FROM agent_acts "
             "WHERE project_id = :p AND code_task_run_id = :r ORDER BY started_at, act_id"),
        {"p": project_id, "r": run_id},
    ).mappings().all()]


# ── the tool log ─────────────────────────────────────────────────────────────────────────────

_EXIT = re.compile(r"(?:exit(?:ed)?(?: with)?(?: code| status)?)[\s:=]+(-?\d+)", re.I)


def call_arg(tool: Any, content: Any, limit: int = ARG_CHARS) -> Optional[str]:
    """A tool call's one-line argument: the command for a shell, the path for a file tool,
    the pattern for a search. Never what was written."""
    name = str(tool or "").strip().lower()
    data = _j(content, None)
    if not isinstance(data, dict):
        return None
    if name in SHELL_TOOLS or ("command" in data and name not in FILE_TOOLS):
        cmd = data.get("command") or data.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        if not cmd:
            return None
        line = " ".join(str(cmd).split())
        return redact_secrets(line[:limit] + ("…" if len(line) > limit else ""))
    for key in ("file_path", "path", "notebook_path", "filename"):
        if data.get(key):
            return str(data[key])[:limit]
    for key in ("pattern", "query", "url"):
        if data.get(key):
            return redact_secrets(str(data[key])[:limit])
    return None


def result_text(content: Any) -> str:
    """A tool result's text: the CLI stores a string, or a list of text blocks."""
    data = _j(content, None)
    if isinstance(data, list):
        parts = [str(b.get("text") or "") if isinstance(b, dict) else str(b) for b in data]
        return "\n".join(p for p in parts if p)
    if isinstance(data, dict):
        for key in ("stderr", "stdout", "output", "text", "error"):
            if data.get(key):
                return str(data[key])
        return json.dumps(data)[:ERROR_CHARS]
    if isinstance(data, str):
        return data
    return str(content or "")


def exit_code_of(output: str) -> Optional[int]:
    m = _EXIT.search(output[:400]) if output else None
    return int(m.group(1)) if m else None


def tool_log(connection: Any, project_id: str, run_id: str, *,
             acts: list[Mapping[str, Any]], limit: int = TOOL_LOG_CAP) -> dict[str, Any]:
    """Every tool call of a build, oldest first, one line each (see the module doc)."""
    act_ids = [str(a["act_id"]) for a in acts if a.get("act_id")]
    rows = connection.execute(
        text("SELECT act_id, direction, tool_name, content, ok, latency_ms, truncated, created_at "
             "FROM agent_turns WHERE project_id = :p AND direction IN ('tool_call', 'tool_result') "
             "AND (code_task_run_id = :r OR act_id = ANY(:a)) "
             "ORDER BY created_at, turn_index, id"),
        {"p": project_id, "r": run_id, "a": act_ids or [""]},
    ).mappings().all()
    calls: list[dict[str, Any]] = []
    waiting: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        act = str(r["act_id"] or "")
        name = str(r["tool_name"] or "")
        if r["direction"] == "tool_call":
            item = {"act_id": act or None, "tool": name or "tool",
                    "arg": call_arg(name, r["content"]), "ok": None, "ms": None,
                    "exit_code": None, "error": None, "output": None,
                    "at": _iso(r["created_at"]), "_t": r["created_at"]}
            calls.append(item)
            waiting.setdefault((act, name), []).append(item)
            continue
        queue = waiting.get((act, name)) or []
        if not queue and not name:
            queue = next((q for (a, _), q in waiting.items() if a == act and q), [])
        if not queue:
            continue
        item = queue.pop(0)
        item["ok"] = bool(r["ok"])
        if r["latency_ms"]:
            item["ms"] = int(r["latency_ms"])
        elif isinstance(r["created_at"], datetime) and isinstance(item["_t"], datetime):
            item["ms"] = max(0, int((r["created_at"] - item["_t"]).total_seconds() * 1000))
        out = redact_secrets(result_text(r["content"]))
        code = exit_code_of(out)
        item["exit_code"] = code
        if not item["ok"] or (code not in (None, 0)):
            item["ok"] = False
            item["error"] = out[:ERROR_CHARS] or "The tool reported an error"
        else:
            item["output"] = out[:OUTPUT_CHARS] or None
        if r["truncated"]:
            item["clipped"] = True
    for c in calls:
        c.pop("_t", None)

    # The verify output search reads: this attempt's, else the nearest earlier attempt's.
    verify = None
    run = connection.execute(
        text("SELECT id, project_id, state, finalize_ctx, updated_at FROM code_task_runs "
             "WHERE id = :r AND project_id = :p"),
        {"r": run_id, "p": project_id},
    ).mappings().first()
    for r in _ancestors(connection, project_id, run) if run else []:
        lines = str((_j(r.get("state"), {}) or {}).get("last_verify_output") or "").splitlines()
        if lines:
            verify = {"run_id": str(r["id"]), "attempt": _attempt_n(r), "own": r["id"] == run_id,
                      "at": _iso(r.get("updated_at")),
                      "output": redact_secrets("\n".join(lines[-VERIFY_LINES:])),
                      "clipped": len(lines) > VERIFY_LINES}
            break
    return {"run_id": run_id, "items": calls[:limit], "total": len(calls),
            "more": len(calls) > limit, "verify": verify}
