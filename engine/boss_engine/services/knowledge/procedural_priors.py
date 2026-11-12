"""Procedural priors v1 (BOSSAPP-1031): the scorekeeper for first moves.

Frontier models invent search discipline on the spot; weak models flail (measured: llama3.1
greping absurdities, 0/24 on the bench). The harness watches which tools strong runs reached
for per KIND of situation, ranks them by how often they led to progress, and hands the ranking
to cheaper models — content-blind. You don't build the use cases, you build the scorekeeper.

Three parts, all deterministic in v1:

  * ``fingerprint`` — a TOTAL function naming the situation: pipeline stage (or ``solo``),
    the repository's dominant language, and whether the agent is still exploring, already
    building, or standing in front of a failing test/command. v2 may add task-kind and
    vectors; v1 stays deterministic on purpose, so a prior can always be replayed and audited.
  * ``build_priors`` — the offline, replay-first fold of ``agent_turns`` into
    ``kg_action_priors`` upserts (one row per org/fingerprint/canonical tool). Idempotent:
    a high-water mark per org in ``system_state`` keeps a re-run from double-counting.
  * the two injection forms behind ``BOSS_PROCEDURAL_PRIORS`` (off | advisory | ordered),
    rendered here and applied by ``NativeCodeAgent._run_tool_loop`` — the native lane only.
    With the knob off, prompts stay BYTE-IDENTICAL to today's.

The stored ``example_excerpt`` is ONE short scrubbed ARGS-SHAPE (key names and value sizes),
never the arguments themselves: the scorekeeper never memorises content.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..engine_contract import BASH, WRITE_TOOLS, canonical_tool_name
from .code_parse import EXTENSIONS
from .learning import days_between, decay, usefulness
from .learning_writeback import error_signature
from .scrub import scrub

LOG = logging.getLogger(__name__)

FINGERPRINT_VERSION = "v1"
#: The knob (config.procedural_priors_mode / BOSS_PROCEDURAL_PRIORS). Anything else reads as off.
MODES = ("off", "advisory", "ordered")
EXPLORATION, BUILDING, POST_TEST_FAILURE = "exploration", "building", "post_test_failure"
SOLO_STAGE = "solo"
UNKNOWN_LANGUAGE = "unknown"
#: An action is advised only once it has been SEEN enough to mean something and scored above
#: a smoothed coin toss ((s+1)/(s+f+2) with learning.py's 180-day half-life).
MIN_OBSERVATIONS = 5
MIN_USEFULNESS = 0.45
TOP_ACTIONS = 5
#: How many tool results ahead (same stage) progress is looked for when crediting an action.
PROGRESS_WINDOW = 6
#: The advisory block's hard ceiling (~300 tokens at the usual ~4 chars/token).
ADVISORY_MAX_CHARS = 1_200
MAX_EXCERPT_CHARS = 200
#: system_state key prefix for the per-org high-water mark (idempotent re-runs).
_HWM_KEY = "kg_action_priors_high_water"

#: The native lane's spelling of each canonical tool, for rendering advice the model can act on.
_NATIVE_SPELLING = {"Read": "read_file", "Write": "write_file", "Edit": "edit_file",
                    "Bash": "bash", "Grep": "grep", "Glob": "glob", "LS": "list_dir"}

_CLASS = re.compile(r"[A-Za-z_][\w.]*(?:Error|Exception)\b")
_NOT_TOKEN = re.compile(r"[^a-z0-9_]+")
_PART = re.compile(r"[|\s]+")
#: What the native toolset prints for a command (`$ cmd\n(exit N, Xms)`), plus the test-runner
#: phrases a CLI lane's transcript shows instead.
_EXIT_NONZERO = re.compile(r"\(exit [1-9]\d*[,)]")
_EXIT_ZERO = re.compile(r"\(exit 0[,)]")
_FAILING_TEXT = re.compile(r"\bFAILED\b|\bTraceback \(most recent call last\)|\b\d+ failed\b")
_PASSING_TEXT = re.compile(r"\b\d+ (?:tests? )?passed\b|\ball tests passed\b", re.IGNORECASE)

_SKIP_DIRS = frozenset({"node_modules", ".git", ".hg", "venv", ".venv", "dist", "build",
                        "__pycache__", "vendor", "target", ".next", "coverage"})
_LANGUAGE_SCAN_CAP = 4_000


def mode(settings: Any) -> str:
    """The configured injection mode; an unknown value is OFF, never a surprise."""
    raw = str(getattr(settings, "procedural_priors_mode", "") or "").strip().lower()
    return raw if raw in MODES else "off"


# ── The fingerprint: a total, deterministic name for a situation ─────────────────────────────

def _part(value: Any, fallback: str) -> str:
    cleaned = _PART.sub("_", str(value or "").strip().lower()).strip("_")
    return cleaned[:64] or fallback


def _error_class(output: Any) -> str:
    """A failing output's class token, via the EXISTING learning_writeback.error_signature
    regexes, normalised to a lowercase identifier (``assertionerror``, ``typeerror``)."""
    signature = error_signature(str(output or ""))
    if not signature:
        return "unknown"
    match = _CLASS.search(signature)
    token = match.group(0).rsplit(".", 1)[-1] if match else signature.split()[0]
    token = _NOT_TOKEN.sub("_", token.lower()).strip("_")
    return token[:48] or "unknown"


def situation(last_error: Any = None, *, wrote: bool = False) -> str:
    """``post_test_failure:<class>`` when the previous tool result was a failing test/command,
    else ``exploration`` (no write yet in this stage) or ``building`` (a write happened)."""
    if str(last_error or "").strip():
        return f"{POST_TEST_FAILURE}:{_error_class(last_error)}"
    return BUILDING if wrote else EXPLORATION


def fingerprint(stage: Any, repo_language: Any, last_error: Any = None, *,
                wrote: bool = False) -> str:
    """The situation's deterministic key: ``v1|<stage>|<language>|<situation>``.

    A TOTAL function: any input — None, empty, unreadable — still names a situation, so a
    prior can never fail the work it advises. v2 may add task-kind and vectors; v1 stays
    deterministic so every key can be replayed and audited.
    """
    return "|".join((FINGERPRINT_VERSION,
                     _part(stage, SOLO_STAGE),
                     _part(repo_language, UNKNOWN_LANGUAGE),
                     situation(last_error, wrote=wrote)))


def repo_language_of(root: Any) -> str:
    """The workspace's dominant language by extension count — the cheap fallback the design
    allows, over the same extensions the code graph parses (code_parse.EXTENSIONS)."""
    counts: dict[str, int] = {}
    try:
        seen = 0
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                seen += 1
                if seen > _LANGUAGE_SCAN_CAP:
                    raise StopIteration
                lang = EXTENSIONS.get(os.path.splitext(name)[1].lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    except StopIteration:
        pass
    except Exception:  # noqa: BLE001 — a prior never fails the work it advises
        return UNKNOWN_LANGUAGE
    if not counts:
        return UNKNOWN_LANGUAGE
    return min(counts, key=lambda lang: (-counts[lang], lang))


# ── What a tool result says about progress ───────────────────────────────────────────────────

def looks_failed(ok: Any, content: Any) -> bool:
    """A failing test/command: the result errored, or its text shows a non-zero exit or a
    test runner's failure line (the native `bash` returns failing tests as ok results)."""
    if ok is False:
        return True
    text_ = str(content or "")
    return bool(_EXIT_NONZERO.search(text_) or _FAILING_TEXT.search(text_))


def shows_success(content: Any) -> bool:
    text_ = str(content or "")
    return bool(_EXIT_ZERO.search(text_) or _PASSING_TEXT.search(text_))


class SituationTracker:
    """The situation state machine, shared by the offline fold and the live loop.

    ``wrote`` flips on the first ok Write/Edit-class result of the stage; ``last_error`` holds
    the PREVIOUS tool result's text only when it was a failing test/command, so the situation
    is ``post_test_failure`` exactly while the agent stands in front of that failure.
    """

    def __init__(self, *, stage: Any = None, repo_language: Any = None) -> None:
        self.stage = stage
        self.repo_language = repo_language
        self.wrote = False
        self.last_error: Optional[str] = None
        self.failed_in_stage = False

    def observe_result(self, tool_name: Any, content: Any, *, ok: Any = True) -> None:
        canonical = canonical_tool_name(tool_name)
        if canonical in WRITE_TOOLS and ok is not False:
            self.wrote = True
        if canonical == BASH and looks_failed(ok, content):
            self.last_error = str(content or "")[:600]
            self.failed_in_stage = True
        else:
            self.last_error = None

    @property
    def key(self) -> str:
        return fingerprint(self.stage, self.repo_language, self.last_error, wrote=self.wrote)


def args_shape(args: Any) -> str:
    """The SHAPE of a call's arguments, never the arguments: sorted key names with each
    value's type and size. Scrubbed anyway (scrub.py), belt and braces."""
    if not isinstance(args, Mapping):
        shape = f"<{type(args).__name__}>"
    else:
        parts = []
        for key in sorted(str(k) for k in args)[:8]:
            value = args.get(key)
            if isinstance(value, str):
                parts.append(f"{key}=<str {len(value)}>")
            elif isinstance(value, bool):
                parts.append(f"{key}=<bool>")
            elif isinstance(value, (int, float)):
                parts.append(f"{key}=<{type(value).__name__}>")
            elif isinstance(value, Mapping):
                parts.append(f"{key}=<dict {len(value)}>")
            elif isinstance(value, (list, tuple)):
                parts.append(f"{key}=<list {len(value)}>")
            else:
                parts.append(f"{key}=<{type(value).__name__}>")
        shape = ", ".join(parts)
    cleaned, _counts = scrub(shape)
    return cleaned[:MAX_EXCERPT_CHARS]


# ── The offline fold: agent_turns -> kg_action_priors ────────────────────────────────────────

@contextmanager
def _tx(engine_or_session: Any):
    """A transaction on an Engine, or the caller's own Connection/session untouched."""
    if isinstance(engine_or_session, Connection) or not hasattr(engine_or_session, "begin"):
        yield engine_or_session
        return
    if hasattr(engine_or_session, "execute"):  # an already-open session with begin()
        yield engine_or_session
        return
    with engine_or_session.begin() as connection:
        yield connection


def _high_water(connection: Any, org_id: str) -> Optional[datetime]:
    raw = connection.execute(
        text("SELECT value->>'high_water' FROM system_state WHERE key = :k"),
        {"k": f"{_HWM_KEY}:{org_id}"}).scalar()
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _set_high_water(connection: Any, org_id: str, at: datetime) -> None:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    connection.execute(text(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES (:k, CAST(:v AS JSONB), now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """),
        {"k": f"{_HWM_KEY}:{org_id}",
         "v": json.dumps({"high_water": at.astimezone(timezone.utc).isoformat()})})


def _run_language(rows: list[Mapping[str, Any]]) -> str:
    """A replayed run's dominant language, from the file paths its tool calls touched —
    the only workspace evidence a transcript carries."""
    counts: dict[str, int] = {}
    for row in rows:
        if row["direction"] != "tool_call":
            continue
        try:
            args = json.loads(row["content"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(args, Mapping):
            continue
        for key in ("file_path", "path", "filename", "file", "target_file"):
            value = args.get(key)
            if isinstance(value, str):
                lang = EXTENSIONS.get(os.path.splitext(value)[1].lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return UNKNOWN_LANGUAGE
    return min(counts, key=lambda lang: (-counts[lang], lang))


def _credit(rows: list[Mapping[str, Any]], index: int, *, failed_before: bool) -> bool:
    """v1's progress credit, correlational and said so: the action at ``rows[index]`` is a
    success when, within the SAME stage and the next ``PROGRESS_WINDOW`` tool results, any of

      * a Write/Edit-class tool result lands ok,
      * a Bash/test tool result shows success (exit 0, tests passed) after a prior failure
        in the stage,
      * the stage or run ends inside the window with the run 'completed' (gates passed).

    Otherwise a failure. This measures correlation with progress, not causation — v1's honest
    approximation; refining attribution is v2 territory.
    """
    stage = rows[index]["stage"]
    failed = failed_before
    results_seen = 0
    for row in rows[index + 1:]:
        if row["stage"] != stage:
            break
        if row["direction"] != "tool_result":
            continue
        results_seen += 1
        if results_seen > PROGRESS_WINDOW:
            return False
        canonical = canonical_tool_name(row["tool_name"])
        ok = row["ok"] is not False
        if canonical in WRITE_TOOLS and ok:
            return True
        if canonical == BASH:
            if failed and shows_success(row["content"]):
                return True
            if looks_failed(row["ok"], row["content"]):
                failed = True
    # The stage (or the run) ended inside the window: the gate's verdict is the credit.
    return str(rows[index]["run_status"] or "") == "completed"


def build_priors(engine_or_session: Any, *, org_id: str,
                 since: Optional[datetime] = None) -> dict:
    """Fold ``agent_turns`` (with their run context) into ``kg_action_priors`` for one org.

    Offline and replay-first: point it at any database that holds transcripts (a bench DB, a
    replica) and it walks each run's turns in order, runs the situation state machine, and
    counts every tool_call as one observation of (fingerprint, action) with ``_credit``'s
    progress definition. Turns outside any run carry no episode and are skipped.

    Idempotent: without ``since`` it resumes from the org's high-water mark (a system_state
    row) and advances it to the newest turn seen, so a re-run never double-counts. An explicit
    ``since`` overrides the mark (a deliberate re-fold) and still advances it.
    """
    org = str(org_id or "").strip()
    if not org:
        raise ValueError("org_id is required")
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    observations = successes = 0
    newest: Optional[datetime] = None
    with _tx(engine_or_session) as connection:
        effective_since = since if since is not None else _high_water(connection, org)
        rows = [dict(r) for r in connection.execute(text(
            """
            SELECT t.code_task_run_id AS run_id, t.stage, t.direction, t.tool_name,
                   t.content, t.ok, t.created_at, t.turn_index, r.status AS run_status
            FROM agent_turns t
            JOIN code_task_runs r ON r.id = t.code_task_run_id
            JOIN projects p ON p.id = t.project_id
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE w.org_id = :org
              AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR t.created_at > :since)
              AND t.direction IN ('tool_call', 'tool_result')
            ORDER BY t.code_task_run_id, t.created_at, t.turn_index, t.id
            """), {"org": org, "since": effective_since}).mappings().all()]
        runs: dict[str, list[dict]] = {}
        for row in rows:
            runs.setdefault(str(row["run_id"]), []).append(row)
            if newest is None or row["created_at"] > newest:
                newest = row["created_at"]
        for run_rows in runs.values():
            language = _run_language(run_rows)
            tracker = SituationTracker(repo_language=language)
            current_stage: Any = object()
            for index, row in enumerate(run_rows):
                if row["stage"] != current_stage:  # a new stage starts a fresh situation
                    current_stage = row["stage"]
                    tracker = SituationTracker(stage=row["stage"] or SOLO_STAGE,
                                               repo_language=language)
                if row["direction"] == "tool_result":
                    tracker.observe_result(row["tool_name"], row["content"], ok=row["ok"])
                    continue
                action = canonical_tool_name(row["tool_name"])
                if not action:
                    continue
                key = (tracker.key, action)
                stat = stats.setdefault(key, {
                    "successes": 0, "failures": 0, "last_seen": row["created_at"],
                    "example_run_id": None, "example_excerpt": None,
                })
                credited = _credit(run_rows, index, failed_before=tracker.failed_in_stage)
                stat["successes" if credited else "failures"] += 1
                observations += 1
                successes += int(credited)
                if row["created_at"] > stat["last_seen"]:
                    stat["last_seen"] = row["created_at"]
                if stat["example_excerpt"] is None:
                    try:
                        args = json.loads(row["content"] or "{}")
                    except (TypeError, ValueError):
                        args = None
                    if args is not None:
                        stat["example_run_id"] = str(row["run_id"])
                        stat["example_excerpt"] = args_shape(args)
        for (fingerprint_key, action), stat in stats.items():
            connection.execute(text(
                """
                INSERT INTO kg_action_priors
                    (id, org_id, fingerprint_key, action, success_count, failure_count,
                     last_seen_at, example_run_id, example_excerpt)
                VALUES (:id, :org, :fp, :action, :s, :f, :seen, :run, :excerpt)
                ON CONFLICT (org_id, fingerprint_key, action) DO UPDATE SET
                    success_count = kg_action_priors.success_count + EXCLUDED.success_count,
                    failure_count = kg_action_priors.failure_count + EXCLUDED.failure_count,
                    last_seen_at = GREATEST(kg_action_priors.last_seen_at, EXCLUDED.last_seen_at),
                    example_run_id = COALESCE(kg_action_priors.example_run_id, EXCLUDED.example_run_id),
                    example_excerpt = COALESCE(kg_action_priors.example_excerpt, EXCLUDED.example_excerpt)
                """),
                {"id": uuid.uuid4().hex, "org": org, "fp": fingerprint_key, "action": action,
                 "s": stat["successes"], "f": stat["failures"], "seen": stat["last_seen"],
                 "run": stat["example_run_id"], "excerpt": stat["example_excerpt"]})
        if newest is not None:
            _set_high_water(connection, org, newest)
    return {
        "org_id": org,
        "fingerprints": len({fp for fp, _ in stats}),
        "actions": len(stats),
        "observations": observations,
        "successes": successes,
        "failures": observations - successes,
        "high_water": newest.isoformat() if newest is not None else None,
    }


# ── Ranking and the two injection forms ──────────────────────────────────────────────────────

def ranked_actions(connection: Any, *, org_id: str, fingerprint_key: str,
                   now: Optional[datetime] = None) -> list[dict]:
    """The situation's advisable actions, best first: learning.py's usefulness
    ((s+1)/(s+f+2)) decayed by the same 180-day half-life since the prior was last seen,
    thresholded (MIN_OBSERVATIONS, MIN_USEFULNESS), top TOP_ACTIONS, alphabetical tiebreak."""
    at = now or datetime.now(timezone.utc)
    out = []
    for row in connection.execute(text(
            """
            SELECT action, success_count, failure_count, last_seen_at
            FROM kg_action_priors WHERE org_id = :org AND fingerprint_key = :fp
            """), {"org": org_id, "fp": fingerprint_key}).mappings().all():
        s, f = int(row["success_count"] or 0), int(row["failure_count"] or 0)
        if s + f < MIN_OBSERVATIONS:
            continue
        weight = decay(usefulness(s, f), days_between(at, row["last_seen_at"]))
        if weight < MIN_USEFULNESS:
            continue
        out.append({"action": str(row["action"]), "observations": s + f, "successes": s,
                    "weight": weight, "share": round(100.0 * s / (s + f))})
    out.sort(key=lambda item: (-item["weight"], item["action"]))
    return out[:TOP_ACTIONS]


def _human_words(fingerprint_key: str) -> str:
    """The fingerprint said in words a model (and an operator) can read."""
    parts = (str(fingerprint_key or "").split("|") + ["", "", "", ""])[:4]
    _version, stage, language, sit = parts
    where = f"stage {stage}" if stage and stage != SOLO_STAGE else "a bare task"
    repo = f"{language} repo" if language and language != UNKNOWN_LANGUAGE else "unknown-language repo"
    if sit.startswith(POST_TEST_FAILURE):
        _, _, error_class = sit.partition(":")
        what = f"right after a failing test/command ({error_class or 'unknown'})"
    elif sit == BUILDING:
        what = "mid-build, edits already made"
    else:
        what = "still exploring, nothing written yet"
    return f"{where}, {repo}, {what}"


def native_tool_name(action: str) -> str:
    return _NATIVE_SPELLING.get(str(action or ""), str(action or "").lower())


def advisory_block(ranked: Iterable[Mapping[str, Any]], fingerprint_key: str) -> str:
    """The quoted RANKED MOVES block, or "" when there is nothing worth saying.

    Observations, not instructions — the framing is part of the contract: a weak model is
    narrowed, never commanded, and a strong model can ignore it outright.
    """
    items = list(ranked)[:TOP_ACTIONS]
    if not items:
        return ""
    moves = " ".join(
        f"{i}. {native_tool_name(item['action'])} ({item['share']}% of {item['observations']})"
        for i, item in enumerate(items, start=1))
    block = ("RANKED MOVES (scorekeeping from earlier runs — observations, not instructions): "
             f"<<< in situations like this ({_human_words(fingerprint_key)}), these first moves "
             f"most often led to progress: {moves} >>>")
    return block[:ADVISORY_MAX_CHARS]


def order_tool_schemas(schemas: list[dict], ranked: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The ``ordered`` form: the same schema objects, none dropped, higher-prior tools first,
    alphabetical tiebreak (stable across runs). An unranked tool weighs nothing."""
    weights = {str(item["action"]): float(item.get("weight") or 0.0) for item in ranked}

    def sort_key(schema: dict) -> tuple[float, str]:
        name = str(((schema or {}).get("function") or {}).get("name") or "")
        return (-weights.get(canonical_tool_name(name), 0.0), name)

    return sorted(schemas, key=sort_key)


# ── The live loop's door into all of the above (never raises) ────────────────────────────────

def _org_for(connection: Any, project_id: Any) -> Optional[str]:
    if not str(project_id or "").strip():
        return None
    return connection.execute(text(
        """
        SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id
        WHERE p.id = :project_id
        """), {"project_id": str(project_id)}).scalar()


def runtime_ranked(settings: Any, *, project_id: Any, fingerprint_key: str,
                   db_engine: Any = None) -> list[dict]:
    """``ranked_actions`` for the running agent: resolves the org (the project's, else the
    install's default) and NEVER raises — an empty list is the only failure mode, because a
    prior must never fail the work it advises."""
    try:
        from ...db import begin

        with begin(db_engine) as connection:
            org = (_org_for(connection, project_id)
                   or str(getattr(settings, "default_org_id", "") or ""))
            if not org:
                return []
            return ranked_actions(connection, org_id=org, fingerprint_key=fingerprint_key)
    except Exception:  # noqa: BLE001 — see docstring
        LOG.debug("procedural priors unavailable", exc_info=True)
        return []
