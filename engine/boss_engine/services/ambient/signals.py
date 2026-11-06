"""One signal inbox: every source's problems in one normalised, deduplicated shape (BOSSAPP-903).

WHY (design §3.3 gaps 1 and 8, §4 L2)
-------------------------------------
Signals sat in about ten stores — app alerts, doctor findings, shepherd escalations, failed and
deadlettered steps, failed builds, stale PRs — each with its own shape and no deduplication
across them, so the shepherd, the doctor and the captain could each raise the same problem and
nothing could rank one against another. `record_signal` is the ONE way a problem enters the
ambient system. Every store keeps its own table and its own screen; a pull ADAPTER
(ambient/adapters.py) copies what matters from it into a `signals` row.

IDEMPOTENT BY FINGERPRINT
-------------------------
A signal's FINGERPRINT is a stable hash of its kind and its subject (`fingerprint(kind, ...)`),
never of the moment it was seen, so a repeat of the same problem COLLIDES (core.stable_dedupe_key's
rule: "a dedupe key is an IDENTITY, not an id"). `record_signal` then:

  * returns the row untouched when this source row (`source_ref`) was already folded in — a sweep
    that sees the same failed step again is a no-op, and a closed problem is never reopened by
    its own stale evidence;
  * counts an occurrence on the OPEN row (new/triaged) or the SUPPRESSED row for the fingerprint;
  * counts it on a recently CLOSED row too, while the occurrence is inside the row's dedupe
    window (a flapping alert is one problem, not twelve);
  * otherwise opens a new row.

One open/suppressed row per fingerprint is a database rule (uq_signals_open_fingerprint) and
writers serialise on an advisory lock of the fingerprint, so two sweeps racing open one row.

LOOP GUARD AT THE DOOR (design §6 rule 2)
-----------------------------------------
A signal caused by an ambient run (`caused_by_run_id`) is one causal level deeper than the work
item that run belongs to. Past the project's `max_spawn_depth` it is recorded SUPPRESSED with the
reason — visible on the inbox and counted, never worked.

Signal text is data (design §9): titles and evidence notes are normalised (format characters
removed) and bounded before they are stored; nothing in a signal widens a grant or a level.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ._common import (clean_text, emit, iso, new_id, normalize_evidence, now, serialize_row)

#: Where a signal came from (design §4 L2), plus the BOSS stores the first adapters read.
SOURCES = frozenset({"github", "ci", "osv", "alert", "doctor", "shepherd", "scan", "schedule",
                     "cost", "kg", "person", "workflow", "build", "pr"})
SEVERITIES = ("info", "low", "medium", "high", "critical")
OPEN_STATUSES = ("new", "triaged")
STATUSES = ("new", "triaged", "merged_into", "suppressed", "done")
#: How many folded-in source refs a row remembers (newest kept): enough for idempotency over a
#: sweep's look-back, bounded so a flapping alert cannot grow a row without end.
MAX_SOURCE_REFS = 200
DEFAULT_DEDUPE_WINDOW = 3600


def fingerprint(kind: str, *subject: Any, project_id: Optional[str] = None) -> str:
    """The stable identity of a problem: its kind and its subject, never its timestamp."""
    from ..core import stable_dedupe_key

    return stable_dedupe_key(f"sig:{kind}", project_id or "", *subject)


def _severity_rank(value: str) -> int:
    return SEVERITIES.index(value) if value in SEVERITIES else 2


def _lock(connection: Connection, project_id: Optional[str], fp: str) -> None:
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": f"boss-signal:{project_id or ''}:{fp}"})


def causal_depth_of_run(connection: Connection, run_id: Optional[str]) -> int:
    """Depth of a signal raised by `run_id`: the depth of the ambient item that run works, + 1.
    A run no ambient item owns is a person's or a workflow's: depth 0."""
    if not run_id:
        return 0
    depth = connection.execute(text(
        "SELECT max(w.causal_depth) FROM work_item_links l JOIN work_items w "
        "ON w.id = l.work_item_id WHERE l.kind IN ('workflow_run', 'code_task') AND l.ref = :r"),
        {"r": run_id}).scalar()
    return 0 if depth is None else int(depth) + 1


def record_signal(connection: Connection, *, project_id: Optional[str], source: str, kind: str,
                  title: str, fingerprint_parts: Iterable[Any] = (),
                  fingerprint_value: Optional[str] = None, subject_kind: Optional[str] = None,
                  subject_id: Optional[str] = None, app: Optional[str] = None,
                  severity: str = "medium", evidence: Iterable[Any] = (),
                  source_ref: Optional[str] = None, observed_at: Any = None,
                  dedupe_window_seconds: int = DEFAULT_DEDUPE_WINDOW,
                  expires_at: Any = None, caused_by_run_id: Optional[str] = None,
                  actor: str = "boss") -> dict[str, Any]:
    """Record one observation of a problem. Returns {"signal": row, "outcome": ...} where outcome
    is 'created' | 'repeat' (an occurrence counted) | 'duplicate' (this source row was already in)
    | 'suppressed' (counted on a suppressed row, or suppressed at the door by the depth guard).

    The fingerprint is `fingerprint_value` when the source has its own stable key, else
    fingerprint(kind, *fingerprint_parts or (subject_kind, subject_id)). Pass `source_ref` (the
    source row's id, plus anything that makes a NEW occurrence distinct, e.g. an alert's fire
    count) whenever there is one: it is what makes re-sweeping a no-op.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown signal source {source!r} (one of {sorted(SOURCES)})")
    kind = clean_text(kind, 80)
    if not kind:
        raise ValueError("a signal needs a kind")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    title = clean_text(title, 300)
    if not title:
        raise ValueError("a signal needs a title")
    parts = tuple(fingerprint_parts) or (subject_kind or "", subject_id or "")
    fp = fingerprint_value or fingerprint(kind, *parts, project_id=project_id)
    observed = observed_at or now()
    refs_in = normalize_evidence(evidence)
    ref = clean_text(source_ref, 300) or None
    _lock(connection, project_id, fp)

    if ref is not None:
        seen = connection.execute(text(
            "SELECT * FROM signals WHERE COALESCE(project_id, '') = COALESCE(:p, '') "
            "AND fingerprint = :f AND source_refs ? :r ORDER BY last_seen_at DESC LIMIT 1"),
            {"p": project_id, "f": fp, "r": ref}).mappings().first()
        if seen is not None:
            return {"signal": _out(seen), "outcome": "duplicate"}

    current = connection.execute(text(
        "SELECT * FROM signals WHERE COALESCE(project_id, '') = COALESCE(:p, '') "
        "AND fingerprint = :f ORDER BY (status IN ('new', 'triaged', 'suppressed')) DESC, "
        "last_seen_at DESC LIMIT 1"), {"p": project_id, "f": fp}).mappings().first()
    if current is not None and current["status"] == "suppressed":
        until = current["suppressed_until"]
        if until is not None and until <= now():
            connection.execute(text(
                "UPDATE signals SET status = 'done', status_reason = 'suppression ended', "
                "updated_at = now() WHERE id = :id"), {"id": current["id"]})
            current = None
    if current is not None:
        live = current["status"] in OPEN_STATUSES or current["status"] == "suppressed"
        window = timedelta(seconds=int(current["dedupe_window_seconds"] or 0))
        if live or observed <= current["last_seen_at"] + window:
            row = _fold(connection, current, ref=ref, severity=severity, evidence=refs_in,
                        observed=observed)
            outcome = "suppressed" if row["status"] == "suppressed" else "repeat"
            return {"signal": _out(row), "outcome": outcome}

    depth = causal_depth_of_run(connection, caused_by_run_id)
    status, reason = "new", None
    if project_id and depth:
        from . import policy

        limit = int(policy.settings_for(connection, project_id)["max_spawn_depth"])
        if depth > limit:
            status, reason = "suppressed", (f"causal depth {depth} is above the limit of {limit}: "
                                            "raised by ambient work too many levels deep")
    signal_id = new_id("sig")
    row = connection.execute(text(
        "INSERT INTO signals (id, project_id, source, kind, subject_kind, subject_id, app, "
        "fingerprint, severity, title, evidence, source_refs, dedupe_window_seconds, status, "
        "status_reason, caused_by_run_id, causal_depth, observed_at, first_seen_at, "
        "last_seen_at, expires_at) VALUES (:id, :p, :src, :k, :sk, :sid, :app, :f, :sev, :t, "
        "CAST(:ev AS JSONB), CAST(:refs AS JSONB), :win, :st, :why, :cb, :d, :obs, :obs, :obs, "
        ":exp) RETURNING *"),
        {"id": signal_id, "p": project_id, "src": source, "k": kind, "sk": subject_kind,
         "sid": clean_text(subject_id, 300) or None, "app": clean_text(app, 120) or None,
         "f": fp, "sev": severity, "t": title, "ev": json.dumps(refs_in),
         "refs": json.dumps([ref] if ref else []), "win": max(0, int(dedupe_window_seconds)),
         "st": status, "why": reason, "cb": caused_by_run_id, "d": depth, "obs": observed,
         "exp": expires_at}).mappings().one()
    emit(connection, project_id, "ambient.signal_recorded", actor, "signal", signal_id,
         {"source": source, "kind": kind, "fingerprint": fp, "severity": severity,
          "status": status, "causal_depth": depth})
    if status == "suppressed":
        emit(connection, project_id, "ambient.guard_refused", actor, "signal", signal_id,
             {"guard": "causal_depth", "reason": reason, "fingerprint": fp})
    return {"signal": _out(row), "outcome": "created" if status == "new" else "suppressed"}


def _fold(connection: Connection, current: Mapping[str, Any], *, ref: Optional[str],
          severity: str, evidence: list[dict[str, Any]], observed: Any) -> Mapping[str, Any]:
    """Count one more occurrence on an existing row: newest evidence first, highest severity."""
    refs = list(current["source_refs"] or [])
    if ref:
        refs = (refs + [ref])[-MAX_SOURCE_REFS:]
    merged = list(evidence) + [e for e in (current["evidence"] or []) if e not in evidence]
    worst = max(current["severity"], severity, key=_severity_rank)
    return connection.execute(text(
        "UPDATE signals SET occurrences = occurrences + 1, last_seen_at = GREATEST(last_seen_at, "
        ":obs), severity = :sev, evidence = CAST(:ev AS JSONB), source_refs = CAST(:refs AS "
        "JSONB), updated_at = now() WHERE id = :id RETURNING *"),
        {"obs": observed, "sev": worst, "ev": json.dumps(merged[:50]), "refs": json.dumps(refs),
         "id": current["id"]}).mappings().one()


def _out(row: Mapping[str, Any]) -> dict[str, Any]:
    return serialize_row(row)


def get(connection: Connection, signal_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM signals WHERE id = :id"),
                             {"id": signal_id}).mappings().first()
    return _out(row) if row is not None else None


def list_signals(connection: Connection, *, project_id: Optional[str],
                 status: Optional[Iterable[str]] = None, source: Optional[str] = None,
                 include_install_wide: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    """A project's signals (and the install-wide ones, which every project's CoS may read)."""
    statuses = [s for s in (status or ()) if s in STATUSES] or list(STATUSES)
    rows = connection.execute(text(
        "SELECT * FROM signals WHERE (project_id = :p OR (:wide AND project_id IS NULL)) "
        "AND status = ANY(:st) AND (CAST(:src AS TEXT) IS NULL OR source = :src) "
        "ORDER BY last_seen_at DESC LIMIT :n"),
        {"p": project_id, "wide": include_install_wide, "st": statuses, "src": source,
         "n": max(1, min(int(limit), 500))}).mappings()
    return [_out(r) for r in rows]


def set_status(connection: Connection, signal_id: str, status: str, *, actor: str = "boss",
               reason: Optional[str] = None, merged_into: Optional[str] = None,
               suppressed_until: Any = None, work_item_id: Optional[str] = None) -> dict[str, Any]:
    """Move a signal on: triaged (a work item holds it), merged_into (another signal is the same
    problem), suppressed (until a time, or until the evidence changes), done."""
    if status not in STATUSES:
        raise ValueError(f"signal status must be one of {', '.join(STATUSES)}")
    if status == "merged_into" and not merged_into:
        raise ValueError("merged_into names the signal this one is the same problem as")
    row = connection.execute(text(
        "UPDATE signals SET status = :s, status_reason = COALESCE(:r, status_reason), "
        "merged_into = COALESCE(:m, merged_into), suppressed_until = :u, "
        "work_item_id = COALESCE(:w, work_item_id), updated_at = now() WHERE id = :id "
        "RETURNING *"),
        {"s": status, "r": reason, "m": merged_into, "u": suppressed_until, "w": work_item_id,
         "id": signal_id}).mappings().first()
    if row is None:
        raise LookupError(f"no signal {signal_id}")
    emit(connection, row["project_id"], "ambient.signal_status", actor, "signal", signal_id,
         {"status": status, "reason": reason, "merged_into": merged_into})
    return _out(row)


# ── adapters and the sweep ───────────────────────────────────────────────────────────────────
#
# AN ADAPTER is `fn(connection, since, project_id) -> Iterable[dict]`: each dict is
# record_signal's keyword arguments. It READS its store and returns observations; it never
# writes. The sweep owns the cursor (signal_adapter_cursors), the savepoint per adapter (one
# broken store never stops the others) and the idempotency (record_signal). There is no daemon:
# the Chief-of-Staff tick (BOSSAPP-904) — or any caller — runs `sweep(connection)`.

Adapter = Callable[[Connection, Any, Optional[str]], Iterable[Mapping[str, Any]]]
ADAPTERS: dict[str, Adapter] = {}
#: Re-read this much before the cursor: a row committed late by a slow transaction is not lost.
CURSOR_OVERLAP = timedelta(minutes=10)
FIRST_LOOKBACK = timedelta(hours=24)


def adapter(name: str) -> Callable[[Adapter], Adapter]:
    """Register a pull adapter under `name` (decorator)."""
    def deco(fn: Adapter) -> Adapter:
        ADAPTERS[name] = fn
        return fn
    return deco


def sweep(connection: Connection, *, project_id: Optional[str] = None,
          only: Optional[Iterable[str]] = None, actor: str = "boss") -> dict[str, Any]:
    """Run every registered adapter once from its cursor. Returns per-adapter counts/errors.

    With `project_id` only that project's rows are read (and install-wide ones, for the doctor),
    under a cursor of its own (`<adapter>@<project>`), so a per-project tick and the install-wide
    sweep never move each other's place."""
    from . import adapters

    adapters.register()  # the built-in adapters, registered once

    names = [n for n in ADAPTERS if not only or n in set(only)]
    report: dict[str, Any] = {}
    for name in names:
        started = now()
        key = f"{name}@{project_id}" if project_id else name
        cursor = connection.execute(text(
            "SELECT cursor_at FROM signal_adapter_cursors WHERE adapter = :a"),
            {"a": key}).scalar()
        since = (cursor - CURSOR_OVERLAP) if cursor else (started - FIRST_LOOKBACK)
        outcomes: dict[str, int] = {}
        error = None
        # THE NEXT CURSOR is the newest `_cursor` the adapter yielded (the column it filtered on),
        # so an adapter that hit its row limit resumes after the last row it read; an adapter
        # that yields no cursor (state-based, like stale PRs) moves to the sweep's start.
        seen: list[Any] = []
        try:
            with connection.begin_nested():
                for observation in ADAPTERS[name](connection, since, project_id):
                    fields = dict(observation)
                    mark = fields.pop("_cursor", None)
                    if mark is not None:
                        seen.append(mark)
                    result = record_signal(connection, actor=actor, **fields)
                    outcomes[result["outcome"]] = outcomes.get(result["outcome"], 0) + 1
        except Exception as exc:  # noqa: BLE001 — one store's failure is reported, not raised
            error = f"{type(exc).__name__}: {exc}"[:500]
            outcomes = {}
        connection.execute(text(
            "INSERT INTO signal_adapter_cursors (adapter, cursor_at, last_run_at, last_recorded, "
            "last_error) VALUES (:a, CASE WHEN CAST(:e AS TEXT) IS NULL THEN "
            "CAST(:c AS TIMESTAMPTZ) END, now(), :n, :e) ON CONFLICT (adapter) DO UPDATE SET "
            "cursor_at = CASE WHEN EXCLUDED.last_error IS NULL THEN EXCLUDED.cursor_at ELSE "
            "signal_adapter_cursors.cursor_at END, last_run_at = now(), "
            "last_recorded = EXCLUDED.last_recorded, last_error = EXCLUDED.last_error"),
            {"a": key, "c": max(seen) if seen else started, "n": outcomes.get("created", 0),
             "e": error})
        report[name] = {"since": iso(since), "outcomes": outcomes, "error": error}
    return report
