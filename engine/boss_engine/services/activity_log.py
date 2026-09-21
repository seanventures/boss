"""Read-only global activity log (BOSS-379).

Unions existing project-scoped rows from ``events`` plus optional projections
of process steps, jobs, deadletters, and receipts. Does not invent a second
event store. Never returns secret material.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError

from .redaction import redact

try:
    from .activity_classify import classify_activity, redact_activity_data
except ImportError:  # pragma: no cover - sibling slice may land later
    classify_activity = None  # type: ignore[assignment]
    redact_activity_data = None  # type: ignore[assignment]


ActivityStatus = str  # ok | failed | waiting | stuck | info
ActivitySeverity = str  # info | warn | error

SOURCES = ("event", "process_step", "job", "deadletter", "receipt",
           "llm_call", "agent_turn", "app_log", "workflow_step", "deployment")

#: The four levels an operator reads at, coarsest last.
#:
#: `status` (ok/failed/waiting/stuck/info) answers "what happened to this piece of work".
#: `level` answers "how loudly should this speak", which is a different question and the one a
#: log viewer is built around. A transcript is mostly VERBOSE by nature; without a level to
#: filter on, turning per-turn capture on would have buried the events people actually read.
LEVELS = ("verbose", "info", "warn", "error")

#: BOSS's own WORK QUEUE — the three sources that describe BOSS operating itself. This is
#: deliberately NOT called "infra": it is a queue, and offering it under that name was the
#: reason an operator asking "did the deploy fail, is the host degraded" was shown
#: deadletters. The queue question and the infrastructure question are different, and they
#: now have different filters.
QUEUE_SOURCES = ("job", "deadletter", "receipt")

#: Backwards-compatible alias. `source=infra` has been a documented value of the public API
#: and is linked to from saved views, so it keeps resolving to the queue rather than 422-ing.
INFRA_SOURCES = QUEUE_SOURCES

#: The INFRASTRUCTURE the work runs on, as opposed to the work: what was deployed where and
#: whether it came up, and the container output of the apps BOSS builds. `app_log` belongs
#: here because it is the only source that carries a preview's or a deployed app's own stdout
#: (see `log_capture` and `preview_log_ingest`), alongside BOSS's own records.
PLATFORM_SOURCES = ("deployment", "app_log")
SORTS = ("created_at", "status", "kind")
ORDERS = ("asc", "desc")
STATUSES = ("ok", "failed", "waiting", "stuck", "info")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "deadlettered")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Rows per source when a Python-side filter or a non-recency sort is in play and the exact
#: bound does not apply. Generous enough that ordinary searches are unaffected, bounded enough
#: that one request cannot load a project's entire history. When a source hits it, the response
#: says so -- see `truncated_note`.
FILTERED_SCAN_CAP = 5_000

_FAILED_MARKERS = ("fail", "error", "deadletter", "rejected", "blocked")
_WAITING_MARKERS = ("waiting", "queued", "lease")
_STUCK_MARKERS = ("stuck", "stalled", "timeout", "expired")
_OK_MARKERS = ("succeeded", "completed", "certified", "accepted")
_OK_PHRASES = ("handoff succeeded",)
_CLOSED_MARKERS = (
    "succeeded",
    "completed",
    "certified",
    "accepted",
    "cancelled",
    "closed",
    "resolved",
    "done",
)
_DROP_KEYS = frozenset({"token", "secret", "password", "api_key", "authorization"})
_SPLIT = re.compile(r"[._\-]+")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _contains_any(haystack: str, markers: tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(marker in lowered for marker in markers)


def _still_open(status_value: str) -> bool:
    if not status_value:
        return True
    lowered = status_value.lower()
    return not any(marker in lowered for marker in _CLOSED_MARKERS)


def _humanize(value: str) -> str:
    words = _SPLIT.sub(" ", value).split()
    return " ".join(word.capitalize() for word in words)


def _redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in _DROP_KEYS:
            continue
        out[key] = _redact_value(value)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _classify_activity_inline(
    row: Optional[Mapping[str, Any]] = None,
) -> dict[str, str]:
    """Fail-closed classifier: names/types only. Never invent a success."""
    source = dict(row or {})
    event_type = _text(source.get("event_type"))
    action = _text(source.get("action"))
    status_value = _text(source.get("status"))
    kind = _text(source.get("kind"))
    haystack = " ".join(
        part for part in (event_type, action, status_value, kind) if part
    )

    if _contains_any(haystack, _FAILED_MARKERS):
        status: ActivityStatus = "failed"
        severity: ActivitySeverity = "error"
    elif _contains_any(haystack, _STUCK_MARKERS) and _still_open(status_value):
        status = "stuck"
        severity = "warn"
    elif _contains_any(haystack, _WAITING_MARKERS):
        status = "waiting"
        severity = "info"
    elif _contains_any(haystack, _OK_MARKERS) or _contains_any(haystack, _OK_PHRASES):
        status = "ok"
        severity = "info"
    else:
        status = "info"
        severity = "info"

    return {
        "status": status,
        "stage": _text(source.get("stage") or source.get("step_key")),
        "severity": severity,
        "title": _humanize(event_type or action or kind or status_value),
    }


def _redact_activity_data_inline(data: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not data or not isinstance(data, Mapping):
        return {}
    return _redact_mapping(data)


def _classify(row: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    fn = classify_activity if callable(classify_activity) else _classify_activity_inline
    result = fn(row)
    if not isinstance(result, Mapping):
        return _classify_activity_inline(row)
    status = _text(result.get("status"))
    if status not in STATUSES:
        # Fail closed: an unexpected classifier must not mint success.
        fallback = _classify_activity_inline(row)
        status = fallback["status"] if fallback["status"] != "ok" else "info"
        return {
            "status": status,
            "stage": _text(result.get("stage") or fallback.get("stage")),
            "severity": _text(result.get("severity") or fallback.get("severity"))
            or "info",
            "title": _text(result.get("title") or fallback.get("title")),
        }
    return {
        "status": status,
        "stage": _text(result.get("stage")),
        "severity": _text(result.get("severity")) or "info",
        "title": _text(result.get("title")),
    }


def _redact_data(data: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    fn = (
        redact_activity_data
        if callable(redact_activity_data)
        else _redact_activity_data_inline
    )
    result = fn(data)
    if not isinstance(result, Mapping):
        return _redact_activity_data_inline(data)
    return dict(result)


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return {}
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _duration_seconds(started: Any, finished: Any) -> Optional[float]:
    """How long a step or a deploy took, or None when it has not finished.

    None rather than 0: a step still running and a step that took no time are different
    facts, and the console reads a blank duration as "still going" while a zero reads as
    "did nothing" — the exact confusion that made `plrr` look green for weeks.
    """
    if started is None or finished is None:
        return None
    try:
        return round((finished - started).total_seconds(), 3)
    except (TypeError, AttributeError):
        return None


def _timestamp_param(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


def _normalize_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        parsed = int(limit)
    except (TypeError, ValueError) as error:
        raise ValueError("limit must be between 1 and 200.") from error
    if parsed < 1 or parsed > MAX_LIMIT:
        raise ValueError("limit must be between 1 and 200.")
    return parsed


def _normalize_offset(offset: Optional[int]) -> int:
    if offset is None:
        return 0
    try:
        parsed = int(offset)
    except (TypeError, ValueError) as error:
        raise ValueError("offset must be >= 0.") from error
    if parsed < 0:
        raise ValueError("offset must be >= 0.")
    return parsed


def _normalize_sort(sort: Optional[str]) -> str:
    value = _text(sort).lower() if sort is not None else "created_at"
    if not value:
        return "created_at"
    if value not in SORTS:
        raise ValueError("sort must be created_at, status, or kind.")
    return value


def _normalize_order(order: Optional[str]) -> str:
    value = _text(order).lower() if order is not None else "desc"
    if not value:
        return "desc"
    if value not in ORDERS:
        raise ValueError("order must be asc or desc.")
    return value


def _is_missing_relation(exc: BaseException) -> bool:
    if isinstance(exc, ProgrammingError):
        return True
    orig = getattr(exc, "orig", None)
    name = type(orig).__name__ if orig is not None else type(exc).__name__
    blob = str(orig or exc).lower()
    return name == "UndefinedTable" or "does not exist" in blob


def _fetch_rows(
    connection: Connection, sql: str, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Run a source query. Missing optional tables are skipped, never 500."""
    begin_nested = getattr(connection, "begin_nested", None)
    nested = None
    if callable(begin_nested):
        try:
            nested = begin_nested()
        except Exception:
            nested = None
    try:
        result = connection.execute(text(sql), dict(params))
        mappings = getattr(result, "mappings", None)
        rows = mappings().all() if callable(mappings) else list(result)
        if nested is not None:
            nested.commit()
        return [dict(row) for row in rows]
    except Exception as exc:
        if nested is not None:
            try:
                nested.rollback()
            except Exception:
                pass
        if _is_missing_relation(exc):
            return []
        raise


def _time_clauses(
    since: Any, until: Any, *, column: str = "created_at"
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    since_value = _timestamp_param(since)
    until_value = _timestamp_param(until)
    if since_value is not None:
        clauses.append(f"{column} >= :since")
        params["since"] = since_value
    if until_value is not None:
        clauses.append(f"{column} <= :until")
        params["until"] = until_value
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    return extra, params


def _matches_q(item: Mapping[str, Any], q: str) -> bool:
    """Does this row contain the operator's text?

    Runs AFTER the source query has already narrowed by full-text index, and is not redundant
    with it. Full-text answers "does this row contain these WORDS, stemmed"; this answers "does
    this row contain this TEXT", which is what someone searching a log means. Confirming here
    also means search correctness never rests on trusting that each loader really applied its
    WHERE clause — a loader that forgot would leak every row into every search.

    `body` is included, or the confirmation would discard exactly the transcript rows the
    content index just found.
    """
    needle = q.casefold()
    haystack = " ".join(
        (
            _text(item.get("kind")),
            _text(item.get("actor")),
            _text(item.get("entity_type")),
            _text(item.get("entity_id")),
            _text(item.get("title")),
            _text(item.get("body")),
        )
    ).casefold()
    return needle in haystack


def _level_for(severity: str, status_value: str) -> str:
    """The level a classified row is read at.

    Derived from the severity the classifier already produces rather than invented alongside
    it: two independent notions of "how bad is this" drift, and then the level filter and the
    status filter disagree about the same row.
    """
    if severity == "error" or status_value == "failed":
        return "error"
    if severity == "warn" or status_value == "stuck":
        return "warn"
    return "info"


def _item(
    *,
    id: str,
    source: str,
    kind: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    created_at: Any,
    data: Mapping[str, Any],
    classify_row: Mapping[str, Any],
    level: Optional[str] = None,
    run_id: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    agent: Optional[str] = None,
    service: Optional[str] = None,
    body: str = "",
    sql_matched: bool = False,
) -> dict[str, Any]:
    classified = _classify(classify_row)
    status_value = classified["status"]
    return {
        "id": id,
        "source": source,
        "kind": kind,
        "status": status_value,
        "level": level or _level_for(classified.get("severity", "info"), status_value),
        "title": classified["title"],
        "actor": actor,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "stage": classified["stage"],
        # The work task and the AI agent, as first-class filters. Both were reachable only by
        # knowing which `entity_id` convention a given source happened to use.
        "run_id": run_id or "",
        # THE WORKFLOW RUN, which is not the work task. A console URL carries both
        # (`?run=crun_…&wfr=wfr_…`) and they scope different things: `run_id` is one coding
        # task, `workflow_run_id` is the whole governed run that task was one step of. Without
        # this, arriving from a workflow run left no way to ask "show me only this run's log".
        "workflow_run_id": workflow_run_id or "",
        "agent": agent or "",
        # WHICH PROCESS SAID THIS. The API and the worker do different jobs — one serves the
        # console, the other runs the pipeline — so "the API is noisy" and "the worker is
        # stuck" are different findings, and reading them interleaved hides both. Empty for
        # sources that genuinely do not know: an agent turn is not recorded with a process, and
        # guessing one would be inventing a fact to fill a column.
        "service": service or "",
        "created_at": _iso(created_at),
        "data": _redact_data(data),
        # The readable body of a turn. Empty for every other source, which have structured
        # `data` instead.
        "body": body,
        # Set when the SOURCE QUERY already applied the search. Python must not filter it a
        # second time: the SQL matched on content that `_matches_q` cannot see, so re-checking
        # would throw away exactly the rows full-text search just found.
        "_sql_matched": sql_matched,
    }


def _load_events(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    extra, params = _time_clauses(since, until)
    search = ""
    if q:
        # Mirrors idx_events_fts. Without it, every text search was a sequential scan of the
        # project's entire history — 46,774 rows on dogfood — filtered in Python afterwards.
        # Identical expression to idx_events_fts, `translate` included.
        search = (" AND to_tsvector('english', translate(coalesce(event_type, '') || ' ' || "
                  "coalesce(actor, '') || ' ' || coalesce(entity_type, '') || ' ' || "
                  "coalesce(entity_id, ''), '._-', '   ')) @@ plainto_tsquery('english', translate(:q, '._-', '   '))")
        params = {**params, "q": q}
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, event_type, actor, entity_type, entity_id, data, created_at
        FROM events
        WHERE project_id = :project_id{extra}{search}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        event_type = _text(row.get("event_type"))
        data = _as_mapping(row.get("data"))
        entity_id = _text(row.get("entity_id"))
        items.append(
            _item(
                id=_text(row.get("id")),
                source="event",
                kind=event_type,
                actor=_text(row.get("actor")),
                entity_type=_text(row.get("entity_type")),
                entity_id=entity_id,
                created_at=row.get("created_at"),
                data=data,
                classify_row={"event_type": event_type, "kind": event_type},
                # An event about a run IS activity for that run — that is what makes the
                # per-project log on the run page complete rather than transcript-only.
                run_id=entity_id if entity_id.startswith("crun_") else "",
                # Every `workflow.*` event is emitted against entity_type "workflow_run" with
                # the run as its id (see workflows._emit_step_event), so the scope is exact
                # here rather than dug out of the payload.
                workflow_run_id=(
                    entity_id if _text(row.get("entity_type")) == "workflow_run" else ""
                ),
                sql_matched=bool(q),
            )
        )
    return items


def _load_process_steps(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, run_id, step_key, role_key, action, status, handoff_from, created_at
        FROM agent_process_steps
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        action = _text(row.get("action"))
        status = _text(row.get("status"))
        step_key = _text(row.get("step_key"))
        items.append(
            _item(
                id=_text(row.get("id")),
                source="process_step",
                kind=action or step_key,
                actor=_text(row.get("role_key") or row.get("handoff_from") or "system"),
                entity_type="agent_process_step",
                entity_id=_text(row.get("id")),
                created_at=row.get("created_at"),
                data={"status": status, "action": action, "step_key": step_key},
                classify_row={
                    "action": action,
                    "status": status,
                    "kind": action or step_key,
                    "step_key": step_key,
                    "stage": step_key,
                },
            )
        )
    return items


def _load_jobs(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    extra, params = _time_clauses(since, until)
    status_list = ", ".join(f"'{name}'" for name in JOB_STATUSES)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, kind, status, task_id, lease_owner, created_at
        FROM jobs
        WHERE project_id = :project_id
          AND status IN ({status_list}){extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        kind = _text(row.get("kind"))
        status = _text(row.get("status"))
        items.append(
            _item(
                id=_text(row.get("id")),
                source="job",
                kind=kind,
                actor=_text(row.get("lease_owner") or "system"),
                entity_type="job",
                entity_id=_text(row.get("id")),
                created_at=row.get("created_at"),
                data={"status": status, "kind": kind},
                classify_row={"kind": kind, "status": status, "event_type": kind},
            )
        )
    return items


def _load_deadletters(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, source_kind, source_id, status, created_at
        FROM deadletters
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        source_kind = _text(row.get("source_kind"))
        status = _text(row.get("status"))
        items.append(
            _item(
                id=_text(row.get("id")),
                source="deadletter",
                kind=source_kind or "deadletter",
                actor="system",
                entity_type="deadletter",
                entity_id=_text(row.get("id")),
                created_at=row.get("created_at"),
                data={"status": status, "source_kind": source_kind},
                classify_row={
                    "event_type": "deadletter",
                    "kind": source_kind or "deadletter",
                    "status": status,
                },
            )
        )
    return items


def _load_receipts(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, effect_kind, status, created_at
        FROM receipts
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        kind = _text(row.get("effect_kind"))
        status = _text(row.get("status"))
        # Status only — never result, error, effect_scope, or other secret material.
        items.append(
            _item(
                id=_text(row.get("id")),
                source="receipt",
                kind=kind,
                actor="system",
                entity_type="receipt",
                entity_id=_text(row.get("id")),
                created_at=row.get("created_at"),
                data={"status": status},
                classify_row={"kind": kind, "status": status, "event_type": kind},
            )
        )
    return items


def _load_llm_calls(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One row per model INVOCATION: which agent, which model, how long, what it cost.

    The summary layer above the transcript. A run's shape — "the engineer ran for eight
    minutes, the reviewer for five, security failed in a millisecond" — is legible here without
    reading a single turn.
    """
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, code_task_run_id, stage, role, engine, model, total_tokens,
               cost_usd_micros, latency_ms, retries, turns, ok, error, created_at
        FROM llm_calls
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        ok = bool(row.get("ok"))
        stage = _text(row.get("stage"))
        role = _text(row.get("role"))
        model = _text(row.get("model"))
        kind = f"llm.{stage or 'call'}"
        data = {
            "stage": stage, "role": role, "model": model,
            "engine": _text(row.get("engine")),
            "total_tokens": int(row.get("total_tokens") or 0),
            "cost_usd": round(int(row.get("cost_usd_micros") or 0) / 1_000_000, 4),
            "latency_ms": int(row.get("latency_ms") or 0),
            "retries": int(row.get("retries") or 0),
            "turns": int(row.get("turns") or 0),
            "ok": ok,
            "error": _text(row.get("error")),
        }
        # A call that failed is an error; one that needed retries got there the hard way and an
        # operator chasing a slow build wants to see it.
        level = "error" if not ok else ("warn" if int(row.get("retries") or 0) else "info")
        items.append(
            _item(
                id=_text(row.get("id")),
                source="llm_call",
                kind=kind,
                actor=role or "agent",
                entity_type="code_task_run",
                entity_id=_text(row.get("code_task_run_id")),
                created_at=row.get("created_at"),
                data=data,
                classify_row={"kind": kind, "status": "ok" if ok else "failed",
                              "stage": stage},
                level=level,
                run_id=_text(row.get("code_task_run_id")),
                agent=role,
                body=f"{model or 'model'} · {int(row.get('turns') or 0)} turns"
                     + (f" · {_text(row.get('error'))}" if not ok else ""),
            )
        )
    return items


def _load_app_logs(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    """The container log, for the operator who cannot open a shell on the box.

    BOSS logged which command ran and for how long, how long a stage queued for a model lane,
    why a preview refused — and none of it reached this screen. A phase reading "27m46s — no
    model call" is accurate and unhelpful: it says the model was not the cost without saying
    what was. These are the lines that say what was.

    Records without a project are included too. An error raised before any run was resolved —
    a bad config, a provider refusing a key — belongs in the operator's log precisely because
    nothing owns it; excluding it would hide the failures that have no home.
    """
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, project_id, run_id, service, level, logger, message, created_at
        FROM app_logs
        WHERE (project_id = :project_id OR project_id IS NULL){extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        raw = (_text(row.get("level")) or "info").lower()
        # Python's names are not this log's names.
        level = {"warning": "warn", "critical": "error", "fatal": "error"}.get(raw, raw)
        if level not in LEVELS:
            level = "info"
        logger_name = _text(row.get("logger")) or "boss"
        short = logger_name.rsplit(".", 1)[-1]
        message = _text(row.get("message")) or ""
        items.append(
            _item(
                id=_text(row.get("id")),
                source="app_log",
                kind=f"log.{short}",
                actor=_text(row.get("service")) or "boss",
                entity_type="code_task_run" if row.get("run_id") else "service",
                entity_id=_text(row.get("run_id")) or _text(row.get("service")) or "boss",
                created_at=row.get("created_at"),
                data={"logger": logger_name, "service": _text(row.get("service")),
                      "level": raw, "message": message},
                classify_row={"kind": f"log.{short}",
                              "status": "failed" if level == "error" else "info"},
                level=level,
                run_id=_text(row.get("run_id")),
                service=_text(row.get("service")),
                body=message,
            )
        )
    return items


def _load_workflow_steps(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One row per workflow STEP attempt — the detail the run rail shows and the log did not.

    Workflow lifecycle already reached this screen as `workflow.*` events, which say a step
    became ready or succeeded but carry none of what an operator opens a log to find: how long
    the step actually took, which attempt this was, and the error text when it failed. That
    lives on `workflow_step_runs` and was readable only through the run page.

    The column is `last_error`, not `error` — `workflow_runs` has `error` and these two have
    been confused before (see apps/api/tests/test_describe_workflow_run_against_real_columns).
    """
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, workflow_run_id, step_id, step_kind, status, iteration, attempt,
               max_attempts, last_error, started_at, completed_at, created_at
        FROM workflow_step_runs
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        status = _text(row.get("status"))
        step_id = _text(row.get("step_id"))
        step_kind = _text(row.get("step_kind"))
        attempt = int(row.get("attempt") or 0)
        last_error = _text(row.get("last_error"))
        # A RETRY IS NOT A CLEAN RUN. A step that succeeded on its third attempt is not the
        # same event as one that succeeded first time, and reading them identically is how a
        # flaky step stays invisible until it finally fails for good.
        if status in ("failed", "deadlettered"):
            level = "error"
        elif attempt > 0 or last_error:
            level = "warn"
        elif status in ("succeeded", "skipped", "cancelled"):
            level = "info"
        else:
            level = "verbose"
        items.append(
            _item(
                id=_text(row.get("id")),
                source="workflow_step",
                kind=f"step.{step_kind}" if step_kind else "step",
                actor="workflow",
                entity_type="workflow_step_run",
                entity_id=_text(row.get("id")),
                created_at=row.get("created_at"),
                data={
                    "step_id": step_id,
                    "step_kind": step_kind,
                    "status": status,
                    "iteration": int(row.get("iteration") or 0),
                    "attempt": attempt,
                    "max_attempts": int(row.get("max_attempts") or 0),
                    "duration_seconds": _duration_seconds(
                        row.get("started_at"), row.get("completed_at")
                    ),
                    "started_at": _iso(row.get("started_at")),
                    "completed_at": _iso(row.get("completed_at")),
                    "last_error": last_error,
                },
                classify_row={
                    "action": f"step {step_id} {status}" if step_id else f"step {status}",
                    "status": status,
                    "kind": step_kind,
                    "stage": step_id,
                },
                level=level,
                workflow_run_id=_text(row.get("workflow_run_id")),
                # The error text is the body so it is searchable and expandable like a log
                # line, rather than folded into `data` where the table cannot show it.
                body=last_error,
            )
        )
    return items


def _load_deployments(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    """What was deployed where, and whether it came up.

    "BOSS infra" used to mean jobs, deadletters and receipts — BOSS's own work queue. An
    operator asking the infrastructure question ("did the deploy fail? is the host degraded?")
    was shown the queue and concluded the log had nothing. This is the source that answers it.
    """
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, app_name, environment, status, target_kind, target_ref, commit_ref,
               repo, failure_reason, created_by, created_at, started_at, finished_at
        FROM deployment_runs
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        status = _text(row.get("status"))
        environment = _text(row.get("environment"))
        failure = _text(row.get("failure_reason"))
        app_name = _text(row.get("app_name"))
        items.append(
            _item(
                id=_text(row.get("id")),
                source="deployment",
                kind=f"deploy.{environment}" if environment else "deploy",
                actor=_text(row.get("created_by")) or "deployer",
                entity_type="deployment_run",
                entity_id=_text(row.get("id")),
                created_at=row.get("created_at"),
                data={
                    "app": app_name,
                    "environment": environment,
                    "status": status,
                    "target_kind": _text(row.get("target_kind")),
                    "target_ref": _text(row.get("target_ref")),
                    "commit": _text(row.get("commit_ref")),
                    "repo": _text(row.get("repo")),
                    "duration_seconds": _duration_seconds(
                        row.get("started_at"), row.get("finished_at")
                    ),
                    "failure_reason": failure,
                },
                classify_row={
                    "action": f"deploy {app_name} to {environment} {status}".strip(),
                    "status": status,
                    "kind": "deployment",
                    "stage": environment,
                },
                level="error" if failure or status == "failed" else "info",
                # The app is the service here. It makes `service=` mean "which app's
                # infrastructure" for deployments, the same way it means "which BOSS process"
                # for BOSS's own records.
                service=app_name,
                body=failure,
            )
        )
    return items


def _load_agent_turns(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    """The conversation itself — what the agent was told, said, ran, and got back.

    This is the source the whole feature exists for. It is also the only one where the search
    term is worth pushing into SQL: `content` is free text over the largest table here, and an
    ILIKE across a month of transcripts is exactly the scan a log viewer must not do.
    """
    extra, params = _time_clauses(since, until)
    search = ""
    if q:
        # Matches the GIN index in db/schema.sql. `plainto_tsquery` treats the operator's words
        # as words rather than as tsquery syntax, so a search for "npm ERR!" is a search and not
        # a syntax error.
        # `translate` must match idx_agent_turns_fts exactly — an expression index is only
        # used for the expression it was built on. It splits dotted/underscored identifiers
        # into words, without which "shepherd" cannot match "shepherd.proposed".
        #
        # BOTH SIDES, or neither works. Splitting only the indexed document left the query
        # asking for the compound token "zzq-activity-unique-abc123", which by construction is
        # not in an index that stores its parts — so a search for the exact text of a row
        # returned nothing.
        search = (" AND to_tsvector('english', translate(coalesce(content, ''), '._-', '   ')) "
                  "@@ plainto_tsquery('english', translate(:q, '._-', '   '))")
        params = {**params, "q": q}
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, code_task_run_id, stage, role_key, engine, model, turn_index,
               direction, tool_name, content, content_bytes, truncated, tokens,
               level, ok, error, created_at
        FROM agent_turns
        WHERE project_id = :project_id{extra}{search}
        ORDER BY created_at DESC, turn_index DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        direction = _text(row.get("direction"))
        tool = _text(row.get("tool_name"))
        role = _text(row.get("role_key"))
        ok = bool(row.get("ok"))
        kind = f"turn.{direction}" + (f".{tool}" if tool else "")
        items.append(
            _item(
                id=_text(row.get("id")),
                source="agent_turn",
                kind=kind,
                actor=role or "agent",
                entity_type="code_task_run",
                entity_id=_text(row.get("code_task_run_id")),
                created_at=row.get("created_at"),
                data={
                    "direction": direction, "tool": tool,
                    "model": _text(row.get("model")),
                    "stage": _text(row.get("stage")),
                    "turn_index": int(row.get("turn_index") or 0),
                    "tokens": int(row.get("tokens") or 0),
                    # A clipped turn must never read as a short one.
                    "truncated": bool(row.get("truncated")),
                    "content_bytes": int(row.get("content_bytes") or 0),
                },
                classify_row={"kind": kind, "status": "ok" if ok else "failed",
                              "stage": _text(row.get("stage"))},
                # The level was decided at write time, from the content. Re-deriving it here
                # from the kind alone would lose everything the body said.
                level=_text(row.get("level")) or "verbose",
                run_id=_text(row.get("code_task_run_id")),
                agent=role,
                body=_text(row.get("content")),
                sql_matched=bool(q),
            )
        )
    return items


def _count_all(connection: Connection, project_id: str, *, since: Any, until: Any) -> int:
    """Total activity rows for the project, across every source.

    Counted rather than inferred from the page. `total` was `len(items)` back when every row
    was loaded; with the sources bounded, that number would be the page size wearing a total's
    name -- the same mistake as the console's "Verified today: 100", which was reporting its
    own fetch limit as a fact about the world.
    """
    extra, params = _time_clauses(since, until)
    total = 0
    # Every source the loaders read, or the count and the list disagree — and a total that is
    # smaller than the rows on screen is worse than no total.
    for table in ("events", "agent_process_steps", "jobs", "deadletters", "receipts",
                  "llm_calls", "agent_turns", "workflow_step_runs", "deployment_runs"):
        rows = _fetch_rows(
            connection,
            f"SELECT count(*) AS n FROM {table} WHERE project_id = :project_id{extra}",
            {"project_id": project_id, **params},
        )
        # A source whose table is absent contributes nothing; _fetch_rows returns [] for it.
        for row in rows:
            total += int(row.get("n") or 0)
    return total


_SOURCE_LOADERS = (
    _load_events,
    _load_process_steps,
    _load_jobs,
    _load_deadletters,
    _load_receipts,
    _load_llm_calls,
    _load_agent_turns,
)

#: Which loader serves which source, so a `source=` filter can skip the other six queries
#: entirely rather than loading everything and discarding it in Python. Reading one run's
#: transcript is the common case and it should not cost a scan of the receipts table.
_LOADERS_BY_SOURCE = {
    "event": _load_events,
    "process_step": _load_process_steps,
    "job": _load_jobs,
    "deadletter": _load_deadletters,
    "receipt": _load_receipts,
    "llm_call": _load_llm_calls,
    "agent_turn": _load_agent_turns,
    "app_log": _load_app_logs,
    "workflow_step": _load_workflow_steps,
    "deployment": _load_deployments,
}


class ActivityLogService:
    """Project-scoped, read-only union of ops activity rows."""

    def __init__(self, settings: Any = None):
        self.settings = settings

    def list_activity(self, connection: Connection, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.query(connection, project_id, **kwargs)

    def list(self, connection: Connection, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.query(connection, project_id, **kwargs)

    @staticmethod
    def query(
        connection: Connection,
        project_id: str,
        *,
        q: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
        entity_type: Optional[str] = None,
        level: Optional[str] = None,
        source: Optional[str] = None,
        run_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
        agent: Optional[str] = None,
        service: Optional[str] = None,
        since: Any = None,
        until: Any = None,
        sort: Optional[str] = "created_at",
        order: Optional[str] = "desc",
        limit: Optional[int] = DEFAULT_LIMIT,
        offset: Optional[int] = 0,
    ) -> dict[str, Any]:
        project = _text(project_id)
        if not project:
            raise ValueError("project_id is required.")
        parsed_limit = _normalize_limit(limit)
        parsed_offset = _normalize_offset(offset)
        parsed_sort = _normalize_sort(sort)
        parsed_order = _normalize_order(order)
        needle = _text(q)
        kind_filter = _text(kind)
        status_filter = _text(status)
        actor_filter = _text(actor)
        entity_type_filter = _text(entity_type)
        level_filter = _text(level).lower()
        source_filter = _text(source).lower()
        run_filter = _text(run_id)
        workflow_run_filter = _text(workflow_run_id)
        agent_filter = _text(agent)
        service_filter = _text(service)
        if level_filter and level_filter not in LEVELS:
            raise ValueError(f"level must be one of: {', '.join(LEVELS)}")
        # GROUPS, NOT SOURCES. Two of these names stand for several sources at once, because
        # they answer one question an operator asks in one word.
        #
        #   queue / infra — BOSS's own work queue. `infra` is kept as the historical spelling
        #                   of this group; it was always the queue, and callers link to it.
        #   platform      — the INFRASTRUCTURE the work runs on: deployments, and the
        #                   container output of the apps BOSS builds and ships.
        if source_filter in ("infra", "queue"):
            selected_sources = list(QUEUE_SOURCES)
        elif source_filter == "platform":
            selected_sources = list(PLATFORM_SOURCES)
        elif source_filter:
            if source_filter not in SOURCES:
                raise ValueError(
                    f"source must be one of: {', '.join(SOURCES)}, "
                    "queue (a.k.a. infra), platform"
                )
            selected_sources = [source_filter]
        else:
            selected_sources = list(SOURCES)

        # HOW MANY ROWS EACH SOURCE MAY RETURN.
        #
        # Every source query used to be unbounded: the five of them loaded 46,774 rows on the
        # dogfood project to display fifty, decoding 33,646 JSONB event payloads in Python on
        # the way. /v1/activity took 14.3 seconds while /v1/dashboard took 0.6.
        #
        # The bound cannot simply be `limit`, because the filters and the sort below run in
        # Python, over rows this method has already loaded. So there are two cases:
        #
        #   EXACT   — no filters, newest-first. The page can only contain rows from each
        #             source's own newest (offset + limit), so that bound changes no answer.
        #   CAPPED  — anything else. A filter can match a row arbitrarily far back, and a sort
        #             by status or kind has no relationship to recency, so the cap is a real
        #             limitation. It is declared in the response rather than hidden: a filtered
        #             search that silently stops at 5,000 rows would report "no failures" for a
        #             project that has them, which is worse than being slow.
        filters_active = bool(
            needle or kind_filter or status_filter or actor_filter or entity_type_filter
            or level_filter or run_filter or workflow_run_filter or agent_filter
            or service_filter
        )
        exact = (
            not filters_active
            and parsed_sort == "created_at"
            and parsed_order == "desc"
        )
        cap = (parsed_offset + parsed_limit) if exact else FILTERED_SCAN_CAP

        items: list[dict[str, Any]] = []
        source_counts: list[int] = []
        for source_name in selected_sources:
            loader = _LOADERS_BY_SOURCE[source_name]
            # Only the transcript loader knows how to search in SQL; the rest ignore `q` and
            # are filtered in Python below, exactly as before.
            loaded = loader(connection, project, since=since, until=until, cap=cap,
                            q=needle or None)
            source_counts.append(len(loaded))
            items.extend(loaded)
        # A source that returned exactly its cap probably had more to give.
        capped = not exact and any(count >= cap for count in source_counts)

        if needle:
            # SQL narrowed; this confirms. See `_matches_q`: the index makes the search fast,
            # this keeps it meaning what an operator thinks it means, and neither step alone is
            # enough.
            items = [item for item in items if _matches_q(item, needle)]
        if level_filter:
            items = [item for item in items if item["level"] == level_filter]
        if run_filter:
            items = [item for item in items if item["run_id"] == run_filter]
        if workflow_run_filter:
            items = [
                item for item in items if item["workflow_run_id"] == workflow_run_filter
            ]
        if agent_filter:
            items = [item for item in items if item["agent"] == agent_filter]
        if service_filter:
            # Rows that do not know their process are EXCLUDED, not kept as a maybe. Asking for
            # "the worker" and being shown rows that merely might be from it is worse than a
            # shorter, honest answer.
            items = [item for item in items if item["service"] == service_filter]
        if kind_filter:
            if kind_filter in SOURCES:
                items = [item for item in items if item["source"] == kind_filter]
            else:
                items = [item for item in items if item["kind"] == kind_filter]
        if status_filter:
            items = [item for item in items if item["status"] == status_filter]
        if actor_filter:
            items = [item for item in items if item["actor"] == actor_filter]
        if entity_type_filter:
            items = [
                item for item in items if item["entity_type"] == entity_type_filter
            ]

        reverse = parsed_order == "desc"
        items.sort(
            key=lambda item: (
                _text(item.get(parsed_sort)),
                _text(item.get("created_at")),
                _text(item.get("id")),
            ),
            reverse=reverse,
        )

        page = [
            # `_sql_matched` is an internal handshake between a loader and the filter below it.
            # Leaking it would put an implementation detail into a payload we intend to show
            # customers, and into every test that asserts on an item's shape.
            {key: value for key, value in item.items() if key != "_sql_matched"}
            for item in items[parsed_offset : parsed_offset + parsed_limit]
        ]

        # `total` used to be len(items), which was only true because every row was loaded.
        # Now the sources are bounded, so it is counted where it can be counted and declared
        # as a floor where it cannot. Reporting a bounded length as a total would tell an
        # operator there are 50 events when there are 33,646.
        if exact:
            total = _count_all(connection, project, since=since, until=until)
            total_is_exact = True
        else:
            total = len(items)
            total_is_exact = not capped

        return {
            "items": page,
            "total": total,
            "total_is_exact": total_is_exact,
            "limit": parsed_limit,
            "offset": parsed_offset,
            **(
                {}
                if total_is_exact
                else {
                    "truncated_note": (
                        f"Searched the most recent {FILTERED_SCAN_CAP} rows per source. "
                        "There may be older matches — narrow the time range to reach them."
                    )
                }
            ),
        }
