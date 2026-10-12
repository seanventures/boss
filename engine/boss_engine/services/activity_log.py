"""Read-only global activity log (BOSS-379): everything BOSS logged, as queries.

Ten streams (events, process steps, jobs, dead-letters, receipts, model calls, agent turns,
app output, workflow steps, deployments), each read through one SQL projection
(`activity_sql`). Filters, keyset paging, the histogram and the facet counts are SQL; the
Python classifier still builds every returned row and is the oracle the SQL level is tested
against. Does not invent a second event store. Never returns secret material.
"""

from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import activity_sql
from .redaction import redact, redact_secrets

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

#: Names that stand for SEVERAL sources at once, because an operator asks for them in one word.
#: Every other accepted name is a single source from `SOURCES`.
SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "infra": QUEUE_SOURCES,
    "queue": QUEUE_SOURCES,
    "platform": PLATFORM_SOURCES,
}
ORDERS = ("asc", "desc")
STATUSES = ("ok", "failed", "waiting", "stuck", "info")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "deadlettered")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

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


def _resolve_sources(source_filter: str) -> list[str]:
    """Turn the `source=` filter into the list of loaders to run.

    Accepts one source, a GROUP name, or a comma-separated set of either — so the console can
    offer the streams as independent toggles rather than a one-of dropdown. "Show me the
    workflow and the deploys but not the model chatter" is a real question, and a single-select
    could only answer it by making the operator look twice.

    An empty filter means every source. So does a set naming all of them, which matters: the
    console sends nothing at all when everything is ticked, and that keeps the request on the
    unfiltered fast path instead of spelling out ten names that mean the same thing.
    """
    if not source_filter:
        return list(SOURCES)
    wanted: list[str] = []
    for raw in source_filter.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        for resolved in SOURCE_GROUPS.get(name, (name,)):
            if resolved not in SOURCES:
                raise ValueError(
                    f"source must be one of: {', '.join(SOURCES)}, "
                    f"{', '.join(sorted(SOURCE_GROUPS))} — got {name!r}"
                )
            if resolved not in wanted:
                wanted.append(resolved)
    # A filter that resolved to nothing is a filter that was only separators. Treating it as
    # "everything" matches the empty case rather than silently returning an empty log.
    return wanted or list(SOURCES)


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


def _normalize_order(order: Optional[str]) -> str:
    value = _text(order).lower() if order is not None else "desc"
    if not value:
        return "desc"
    if value not in ORDERS:
        raise ValueError("order must be asc or desc.")
    return value


def _fetch_rows(
    connection: Connection, sql: str, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Run one read. No savepoint around it: a stream whose table is absent is never queried
    (`_missing_sources` asks first), so there is no failure here to contain — and a savepoint
    is two more round trips on every read of a log that is polled every five seconds."""
    result = connection.execute(text(sql), dict(params))
    mappings = getattr(result, "mappings", None)
    rows = mappings().all() if callable(mappings) else list(result)
    return [dict(row) for row in rows]


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


_REDACTED_FIELDS = frozenset({"body", "data"})


def _is_item(value: Mapping[str, Any]) -> bool:
    return "_sql_matched" in value


def scrub(value: Any) -> Any:
    """Every string a log read returns, credentials masked — titles, ids, labels, data, body.

    The one gate every log read passes (list, row, context, export, labels): a key that slipped
    past capture is never shown, whichever field it landed in. PII masking stays on the body
    and data only, where it was before (it would mangle ids and times)."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        # `body` and `data` were already through the full redactor (credentials + PII) when
        # the item was built; every other field passes here.
        return {k: (v if k in _REDACTED_FIELDS and _is_item(value) else scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


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
    return scrub({
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
        # The readable body of a turn, an error or a log line — redacted here too, not only
        # where it was written: an export or a copied line must never be the first place a
        # key that slipped past capture is shown.
        "body": redact(body)[0] if body else "",
        # Set when the SOURCE QUERY already applied the search. Python must not filter it a
        # second time: the SQL matched on content that `_matches_q` cannot see, so re-checking
        # would throw away exactly the rows full-text search just found.
        "_sql_matched": sql_matched,
    })




#: Where a step's output names the work task it started or adopted. A build step records it
#: flat, an awaiting step under `await`, an adopted build under `run_id`.
_STEP_WORK_KEYS = ("code_task_run_id", "run_id", "build_run_id")


def _step_work_ids(connection: Connection, project_id: str, workflow_run_id: str,
                   step_id: str) -> set[str]:
    """The work tasks (crun_…) one workflow step started, across every attempt and iteration."""
    rows = _fetch_rows(
        connection,
        """
        SELECT workflow_run_id, step_id, output FROM workflow_step_runs
        WHERE project_id = :project_id AND workflow_run_id = :w AND step_id = :s
        """,
        {"project_id": project_id, "w": workflow_run_id, "s": step_id},
    )
    found: set[str] = set()
    for row in rows:
        if _text(row.get("workflow_run_id")) != workflow_run_id or _text(row.get("step_id")) != step_id:
            continue
        out = _as_mapping(row.get("output"))
        for key in _STEP_WORK_KEYS:
            value = _text(out.get(key))
            if value.startswith("crun_"):
                found.add(value)
        awaited = _as_mapping(out.get("await"))
        if _text(awaited.get("kind")) == "code_task" and _text(awaited.get("id")).startswith("crun_"):
            found.add(_text(awaited.get("id")))
    return found



def _encode_cursor(item: Mapping[str, Any]) -> str:
    return f"{_text(item.get('created_at'))}|{_text(item.get('id'))}"


def _decode_cursor(cursor: str) -> tuple[str, str]:
    stamp, sep, row_id = cursor.partition("|")
    if not sep or not stamp.strip():
        raise ValueError("cursor must be the next_cursor value from a previous page.")
    return stamp.strip(), row_id.strip()




# ── One row per source, from the SQL projection ──────────────────────────────────────────────
#
# Each builder turns one row of `activity_sql.SOURCES[<source>]`'s projection — `raw` (the
# source columns as JSON), `ts` (when it happened), `started`/`finished` — into the item every
# client reads. These are the bodies of the old per-source loaders, unchanged: the item shape,
# the redaction and the Python classifier (the oracle the SQL level is tested against) are
# exactly what they were.


def _raw(row: Mapping[str, Any]) -> dict[str, Any]:
    return _as_mapping(row.get("raw"))


def _build_event(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = _raw(row)
    event_type = _text(raw.get("event_type"))
    entity_id = _text(raw.get("entity_id"))
    return _item(
        id=_text(row.get("id")), source="event", kind=event_type,
        actor=_text(raw.get("actor")), entity_type=_text(raw.get("entity_type")),
        entity_id=entity_id, created_at=row.get("ts"), data=_as_mapping(raw.get("data")),
        classify_row={"event_type": event_type, "kind": event_type},
        # An event about a run IS activity for that run — that is what makes the per-project
        # log on the run page complete rather than transcript-only.
        run_id=entity_id if entity_id.startswith("crun_") else "",
        # Every `workflow.*` event is emitted against entity_type "workflow_run" with the run as
        # its id (see workflows._emit_step_event), so the scope is exact here.
        workflow_run_id=entity_id if _text(raw.get("entity_type")) == "workflow_run" else "",
        sql_matched=bool(row.get("_fts")),
    )


def _build_process_step(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = _raw(row)
    action, status, step_key = (_text(raw.get("action")), _text(raw.get("status")),
                                _text(raw.get("step_key")))
    return _item(
        id=_text(row.get("id")), source="process_step", kind=action or step_key,
        actor=_text(raw.get("role_key") or raw.get("handoff_from") or "system"),
        entity_type="agent_process_step", entity_id=_text(row.get("id")),
        created_at=row.get("ts"),
        data={"status": status, "action": action, "step_key": step_key},
        classify_row={"action": action, "status": status, "kind": action or step_key,
                      "step_key": step_key, "stage": step_key},
    )


def _build_job(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = _raw(row)
    kind, status = _text(raw.get("kind")), _text(raw.get("status"))
    return _item(
        id=_text(row.get("id")), source="job", kind=kind,
        actor=_text(raw.get("lease_owner") or "system"), entity_type="job",
        entity_id=_text(row.get("id")), created_at=row.get("ts"),
        data={"status": status, "kind": kind},
        classify_row={"kind": kind, "status": status, "event_type": kind},
    )


def _build_deadletter(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = _raw(row)
    source_kind, status = _text(raw.get("source_kind")), _text(raw.get("status"))
    return _item(
        id=_text(row.get("id")), source="deadletter", kind=source_kind or "deadletter",
        actor="system", entity_type="deadletter", entity_id=_text(row.get("id")),
        created_at=row.get("ts"), data={"status": status, "source_kind": source_kind},
        classify_row={"event_type": "deadletter", "kind": source_kind or "deadletter",
                      "status": status},
    )


def _build_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = _raw(row)
    kind, status = _text(raw.get("effect_kind")), _text(raw.get("status"))
    # Status only — never result, error, effect_scope, or other secret material.
    return _item(
        id=_text(row.get("id")), source="receipt", kind=kind, actor="system",
        entity_type="receipt", entity_id=_text(row.get("id")), created_at=row.get("ts"),
        data={"status": status},
        classify_row={"kind": kind, "status": status, "event_type": kind},
    )


def _build_llm_call(row: Mapping[str, Any]) -> dict[str, Any]:
    """One row per model INVOCATION: which agent, which model, how long, what it cost."""
    raw = _raw(row)
    ok = bool(raw.get("ok"))
    stage, role, model = _text(raw.get("stage")), _text(raw.get("role")), _text(raw.get("model"))
    kind = f"llm.{stage or 'call'}"
    retries = int(raw.get("retries") or 0)
    turns = int(raw.get("turns") or 0)
    error = _text(raw.get("error"))
    data = {
        "stage": stage, "role": role, "model": model, "engine": _text(raw.get("engine")),
        "total_tokens": int(raw.get("total_tokens") or 0),
        "cost_usd": round(int(raw.get("cost_usd_micros") or 0) / 1_000_000, 4),
        "latency_ms": int(raw.get("latency_ms") or 0), "retries": retries, "turns": turns,
        "ok": ok, "error": error,
        # A subscription call is not money (test_a_subscription_call_is_not_money): the row
        # says so, so no reader shows its list price as spend.
        "included_in_subscription": bool(raw.get("included_in_subscription")),
        "act_id": _text(raw.get("act_id")),
    }
    # A call that failed is an error; one that needed retries got there the hard way.
    level = "error" if not ok else ("warn" if retries else "info")
    return _item(
        id=_text(row.get("id")), source="llm_call", kind=kind, actor=role or "agent",
        entity_type="code_task_run", entity_id=_text(raw.get("code_task_run_id")),
        created_at=row.get("ts"), data=data,
        classify_row={"kind": kind, "status": "ok" if ok else "failed", "stage": stage},
        level=level, run_id=_text(raw.get("code_task_run_id")), agent=role,
        body=f"{model or 'model'} · {turns} turns" + (f" · {error}" if not ok else ""),
    )


def _build_app_log(row: Mapping[str, Any]) -> dict[str, Any]:
    """The container log, for the operator who cannot open a shell on the box."""
    raw = _raw(row)
    level_raw = (_text(raw.get("level")) or "info").lower()
    level = {"warning": "warn", "critical": "error", "fatal": "error"}.get(level_raw, level_raw)
    if level not in LEVELS:
        level = "info"
    logger_name = _text(raw.get("logger")) or "boss"
    short = logger_name.rsplit(".", 1)[-1]
    message = _text(raw.get("message")) or ""
    return _item(
        id=_text(row.get("id")), source="app_log", kind=f"log.{short}",
        actor=_text(raw.get("service")) or "boss",
        entity_type="code_task_run" if raw.get("run_id") else "service",
        entity_id=_text(raw.get("run_id")) or _text(raw.get("service")) or "boss",
        created_at=row.get("ts"),
        data={"logger": logger_name, "service": _text(raw.get("service")),
              "level": level_raw, "message": message},
        classify_row={"kind": f"log.{short}", "status": "failed" if level == "error" else "info"},
        level=level, run_id=_text(raw.get("run_id")), service=_text(raw.get("service")),
        body=message,
    )


def _build_workflow_step(row: Mapping[str, Any]) -> dict[str, Any]:
    """One row per workflow STEP attempt, dated when it ran (not when it was planned)."""
    raw = _raw(row)
    status, step_id = _text(raw.get("status")), _text(raw.get("step_id"))
    step_kind = _text(raw.get("step_kind"))
    attempt = int(raw.get("attempt") or 0)
    last_error = _text(raw.get("last_error"))
    # A RETRY IS NOT A CLEAN RUN; `attempt` is 1 on a first run, so only a second is a retry.
    if status in ("failed", "deadlettered"):
        level = "error"
    elif attempt > 1 or (last_error and status != "succeeded"):
        level = "warn"
    elif status in ("succeeded", "skipped", "cancelled"):
        level = "info"
    else:
        level = "verbose"
    return _item(
        id=_text(row.get("id")), source="workflow_step",
        kind=f"step.{step_kind}" if step_kind else "step", actor="workflow",
        entity_type="workflow_step_run", entity_id=_text(row.get("id")),
        created_at=row.get("ts"),
        data={
            "step_id": step_id, "step_kind": step_kind, "status": status,
            "iteration": int(raw.get("iteration") or 0), "attempt": attempt,
            "max_attempts": int(raw.get("max_attempts") or 0),
            "duration_seconds": _duration_seconds(row.get("started"), row.get("finished")),
            "started_at": _iso(row.get("started")), "completed_at": _iso(row.get("finished")),
            "last_error": last_error,
        },
        classify_row={"action": f"step {step_id} {status}" if step_id else f"step {status}",
                      "status": status, "kind": step_kind, "stage": step_id},
        level=level, workflow_run_id=_text(raw.get("workflow_run_id")), body=last_error,
    )


def _build_deployment(row: Mapping[str, Any]) -> dict[str, Any]:
    """What was deployed where, and whether it came up — one line for the deploy, and one per
    step it recorded (`activity_sql._DEPLOY_LINES`). The level mirrors `_DEPLOY_LEVEL`."""
    raw = _raw(row)
    status, environment = _text(raw.get("status")), _text(raw.get("environment"))
    failure, app_name = _text(raw.get("failure_reason")), _text(raw.get("app_name"))
    deploy_id = _text(raw.get("deploy_id")) or _text(row.get("id"))
    common = {
        "app": app_name, "environment": environment, "status": status,
        "target_kind": _text(raw.get("target_kind")),
        "target_ref": _text(raw.get("target_ref")), "commit": _text(raw.get("commit_ref")),
        "repo": _text(raw.get("repo")),
        "duration_seconds": _duration_seconds(row.get("started"), row.get("finished")),
        "failure_reason": failure, "deploy_id": deploy_id,
    }
    if _text(raw.get("line")) == "step":
        label, log = _text(raw.get("label")), _text(raw.get("log"))
        lowered = status.lower()
        if lowered in ("failed", "error") or failure:
            level = "error"
        elif lowered in ("pending", "skipped", ""):
            level = "verbose"
        else:
            level = "info"
        return _item(
            id=_text(row.get("id")), source="deployment", kind="deploy.step",
            actor=_text(raw.get("created_by")) or "deployer", entity_type="deployment_run",
            entity_id=deploy_id, created_at=row.get("ts"),
            data={**common, "step": label, "order": int(raw.get("ord") or 0)},
            classify_row={"action": f"deploy step {label} {status}".strip(),
                          "status": status, "kind": "deployment", "stage": label},
            level=level, body=log or failure,
        )
    return _item(
        id=_text(row.get("id")), source="deployment",
        kind=f"deploy.{environment}" if environment else "deploy",
        actor=_text(raw.get("created_by")) or "deployer", entity_type="deployment_run",
        entity_id=deploy_id, created_at=row.get("ts"),
        data=common,
        classify_row={"action": f"deploy {app_name} to {environment} {status}".strip(),
                      "status": status, "kind": "deployment", "stage": environment},
        level="error" if failure or status == "failed" else "info",
        # No process: the app is named in the row and found by `subject=` (D16). A stack deploy
        # carries its command log on this line (its steps record no log of their own).
        body="\n".join(part for part in (failure, _text(raw.get("log"))) if part),
    )


def _build_agent_turn(row: Mapping[str, Any]) -> dict[str, Any]:
    """The conversation itself — what the agent was told, said, ran, and got back."""
    raw = _raw(row)
    direction, tool = _text(raw.get("direction")), _text(raw.get("tool_name"))
    role = _text(raw.get("role_key"))
    ok = bool(raw.get("ok"))
    kind = f"turn.{direction}" + (f".{tool}" if tool else "")
    return _item(
        id=_text(row.get("id")), source="agent_turn", kind=kind, actor=role or "agent",
        entity_type="code_task_run", entity_id=_text(raw.get("code_task_run_id")),
        created_at=row.get("ts"),
        data={
            "direction": direction, "tool": tool, "model": _text(raw.get("model")),
            "stage": _text(raw.get("stage")), "turn_index": int(raw.get("turn_index") or 0),
            "tokens": int(raw.get("tokens") or 0),
            # A clipped turn must never read as a short one.
            "truncated": bool(raw.get("truncated")),
            "content_bytes": int(raw.get("content_bytes") or 0),
            "act_id": _text(raw.get("act_id")),
        },
        classify_row={"kind": kind, "status": "ok" if ok else "failed",
                      "stage": _text(raw.get("stage"))},
        # The level was decided at write time, from the content.
        level=_text(raw.get("level")) or "verbose",
        run_id=_text(raw.get("code_task_run_id")), agent=role,
        body=_text(raw.get("content")), sql_matched=bool(row.get("_fts")),
    )


_BUILDERS = {
    "event": _build_event, "process_step": _build_process_step, "job": _build_job,
    "deadletter": _build_deadletter, "receipt": _build_receipt, "llm_call": _build_llm_call,
    "agent_turn": _build_agent_turn, "app_log": _build_app_log,
    "workflow_step": _build_workflow_step, "deployment": _build_deployment,
}


def _build(row: Mapping[str, Any]) -> dict[str, Any]:
    item = _BUILDERS[_text(row.get("src"))](row)
    item.pop("_sql_matched", None)
    return item


# ── The query, as a value ────────────────────────────────────────────────────────────────────

#: The relative windows an operator picks. 30 days is the floor of the retention for turns and
#: app output, so a longer range would quietly show a log with holes in it.
RANGES: dict[str, timedelta] = {
    "15m": timedelta(minutes=15), "1h": timedelta(hours=1), "24h": timedelta(hours=24),
    "7d": timedelta(days=7), "30d": timedelta(days=30),
}

#: Histogram bucket per window: 60-120 bars whatever the range.
_BUCKETS = ((timedelta(minutes=15), 15), (timedelta(hours=1), 60),
            (timedelta(hours=24), 900), (timedelta(days=7), 7200), (timedelta(days=30), 21600))

#: Export is the current filter, bounded: a log file is not a database dump.
EXPORT_MAX_ROWS = 50_000
EXPORT_MAX_DAYS = 30

#: The sentence a non-time sort is refused with: level and status are facets now, and a sort
#: by them over a paged log only ever sorted the page.
SORT_REFUSED = "Sort by time; use filters for level or status"


def _split(value: Any) -> list[str]:
    out: list[str] = []
    for part in _text(value).split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def _parse_time(value: Any, name: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text_value = _text(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp.") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class LogQuery:
    """Every filter the log understands, parsed once and shared by list, counts and export."""

    project_id: str
    q: str = ""
    kinds: list[str] = field(default_factory=list)
    status: str = ""
    actor: str = ""
    entity_type: str = ""
    levels: list[str] = field(default_factory=list)
    exclude_levels: list[str] = field(default_factory=list)
    hide_verbose: bool = False
    sources: list[str] = field(default_factory=list)
    exclude_sources: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    exclude_agents: list[str] = field(default_factory=list)
    service: str = ""
    run_id: str = ""
    workflow_run_id: str = ""
    step_id: str = ""
    act: str = ""
    subject: str = ""
    #: What `subject` stands for, resolved once (`_subject_ids`): the subject itself, the id in
    #: a deploy ref, and the records a deploy is made of (its job, decision, ChangeSet, receipts).
    subjects: list[str] = field(default_factory=list)
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    range: str = ""
    work: list[str] = field(default_factory=list)
    #: Streams whose table this database does not have (a partial schema): never queried.
    missing: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, project_id: str, *, now: Optional[datetime] = None, **params: Any) -> "LogQuery":
        project = _text(project_id)
        if not project:
            raise ValueError("project_id is required.")
        levels = [v.lower() for v in _split(params.get("level"))]
        exclude_levels = [v.lower() for v in _split(params.get("exclude_level"))]
        for value in levels + exclude_levels:
            if value not in LEVELS:
                raise ValueError(f"level must be one of: {', '.join(LEVELS)}")
        status = _text(params.get("status")).lower()
        if status and status not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        source_filter = _text(params.get("source")).lower()
        sources = _resolve_sources(source_filter) if source_filter else []
        if sources and set(sources) == set(SOURCES):
            sources = []
        exclude_sources = (_resolve_sources(_text(params.get("exclude_source")).lower())
                           if _text(params.get("exclude_source")) else [])
        query = cls(
            project_id=project, q=_text(params.get("q")), kinds=_split(params.get("kind")),
            status=status, actor=_text(params.get("actor")),
            entity_type=_text(params.get("entity_type")), levels=levels,
            exclude_levels=exclude_levels,
            hide_verbose=str(params.get("hide_verbose") or "").lower() in ("1", "true", "yes"),
            sources=sources, exclude_sources=exclude_sources,
            agents=_split(params.get("agent")), exclude_agents=_split(params.get("exclude_agent")),
            service=_text(params.get("service")), run_id=_text(params.get("run_id")),
            workflow_run_id=_text(params.get("workflow_run_id")),
            step_id=_text(params.get("step_id")), act=_text(params.get("act")),
            subject=_text(params.get("subject")),
            since=_parse_time(params.get("since"), "since"),
            until=_parse_time(params.get("until"), "until"),
        )
        if query.step_id and not query.workflow_run_id:
            raise ValueError("step_id needs workflow_run_id: a step id is only unique within its run.")
        window = _text(params.get("range")).lower()
        if window:
            if window not in RANGES:
                raise ValueError(f"range must be one of: {', '.join(RANGES)}")
            query.range = window
            if query.since is None:
                query.since = (now or datetime.now(timezone.utc)) - RANGES[window]
        return query

    @property
    def scoped(self) -> bool:
        return bool(self.run_id or self.workflow_run_id or self.step_id or self.act
                    or self.subject)

    def selected(self) -> list[str]:
        chosen = self.sources or list(SOURCES)
        return [name for name in chosen
                if name not in self.exclude_sources and name not in self.missing]

    def window(self) -> dict[str, Any]:
        return {"from": _iso(self.since) or None, "to": _iso(self.until) or None,
                "range": self.range or None}

    def key(self) -> str:
        """The filters, as a stable string — part of every ETag (the resolved window is not:
        a relative range's edges move every second, and a quiet log must stay 304)."""
        parts = {k: v for k, v in sorted(self.__dict__.items())
                 if k not in ("since", "until", "work", "missing", "subjects")
                 and v not in ("", [], None, False, set())}
        if not self.range:
            parts["since"], parts["until"] = _iso(self.since), _iso(self.until)
        return json.dumps(parts, sort_keys=True, default=str)


def _kind_plan(query: LogQuery, source: str) -> Optional[str]:
    """How `kind=` narrows one source: '' (no clause), a clause, or None (skip the source).

    A kind that names a SOURCE has always meant that source (`kind=deadletter`), so a set may
    mix the two: a row matches when its source is named or its kind is."""
    if not query.kinds:
        return ""
    named_sources = [k for k in query.kinds if k in SOURCES]
    real_kinds = [k for k in query.kinds if k not in SOURCES]
    if source in named_sources:
        return ""
    if real_kinds:
        return f"{activity_sql.SOURCES[source].kind} = ANY(:kinds)"
    return None


def _where(query: LogQuery, source: str, *, skip: frozenset[str] = frozenset(),
           time_only: bool = False) -> Optional[list[str]]:
    """The WHERE for one source, or None when no row of it can match (it is not queried).

    `skip` leaves a facet's own filter out, which is how a facet counts what selecting each of
    its values would show."""
    spec = activity_sql.SOURCES[source]
    clauses: list[str] = [spec.always] if spec.always else []
    if query.since is not None:
        clauses.append(f"{spec.ts} >= :since")
    if query.until is not None:
        clauses.append(f"{spec.ts} <= :until")
    if time_only:
        return clauses
    if "level" not in skip:
        if query.levels:
            clauses.append(f"{spec.level} = ANY(:levels)")
        elif query.hide_verbose:
            clauses.append(f"{spec.level} <> 'verbose'")
        if query.exclude_levels:
            clauses.append(f"{spec.level} <> ALL(:exclude_levels)")
    if "status" not in skip and query.status:
        clauses.append(f"{spec.status} = :status")
    if query.actor:
        clauses.append(f"{spec.actor} = :actor")
    if query.entity_type:
        clauses.append(f"{spec.entity_type} = :entity_type")
    kind_clause = _kind_plan(query, source)
    if kind_clause is None:
        return None
    if kind_clause:
        clauses.append(kind_clause)
    if "agent" not in skip:
        if query.agents:
            if spec.agent is None:
                return None
            clauses.append(f"{spec.agent} = ANY(:agents)")
        if query.exclude_agents and spec.agent is not None:
            clauses.append(f"coalesce({spec.agent}, '') <> ALL(:exclude_agents)")
    if "service" not in skip and query.service:
        if spec.service is not None:
            clauses.append(f"{spec.service} = :service")
        elif spec.service_match is not None:
            clauses.append(spec.service_match)
        else:
            return None
    if query.run_id:
        if spec.run_match is None:
            return None
        clauses.append(f"({spec.run_match})")
    if query.step_id:
        options = []
        if spec.step_match:
            options.append(f"({spec.step_match})")
        if spec.work_match and query.work:
            options.append(f"({spec.work_match})")
        if not options:
            return None
        clauses.append("(" + " OR ".join(options) + ")")
    elif query.workflow_run_id:
        if spec.wfr_match is None:
            return None
        clauses.append(f"({spec.wfr_match})")
    if query.act:
        if spec.act_match is None:
            return None
        clauses.append(f"({spec.act_match})")
    if query.subject:
        clauses.append(f"({spec.subject_match or 't.id = ANY(:subjects)'})")
    if query.q:
        clauses.append(f"({spec.fts})" if spec.fts else f"strpos({spec.text}, lower(:q)) > 0")
    return clauses


def _params(query: LogQuery) -> dict[str, Any]:
    return {
        "project_id": query.project_id, "q": query.q, "kinds": query.kinds,
        "status": query.status, "actor": query.actor, "entity_type": query.entity_type,
        "levels": query.levels, "exclude_levels": query.exclude_levels,
        "agents": query.agents, "exclude_agents": query.exclude_agents,
        "service": query.service, "run_id": query.run_id, "wfr": query.workflow_run_id,
        "step": query.step_id, "work": query.work, "act": query.act, "subject": query.subject,
        "subjects": query.subjects or ([query.subject] if query.subject else []),
        "since": query.since, "until": query.until,
    }


def _branches(query: LogQuery, select: str, *, sources: Optional[list[str]] = None,
              skip: frozenset[str] = frozenset(), extra: str = "", order: str = "",
              limit: str = "", time_only: bool = False) -> list[str]:
    """One SELECT per (source, scope part) that can match, ready for UNION ALL.

    `select` is a template over the source spec: `{src}`, `{ts}`, `{level}` … are filled in."""
    out: list[str] = []
    for name in sources if sources is not None else query.selected():
        if name in query.missing:
            continue
        spec = activity_sql.SOURCES[name]
        clauses = _where(query, name, skip=skip, time_only=time_only)
        if clauses is None:
            continue
        for scope in spec.scopes:
            where = " AND ".join([scope, *clauses, *([extra.format(ts=spec.ts)] if extra else [])])
            columns = select.format(
                src=f"'{name}'", ts=spec.ts, level=spec.level, status=spec.status,
                raw=spec.raw, started=spec.started, finished=spec.finished,
                agent=spec.agent or "NULL::text", service=spec.service or "NULL::text",
                run_key=spec.run_key, kind=spec.kind or "NULL::text",
                fts=(spec.fts if (spec.fts and query.q) else "false"))
            sql = f"SELECT {columns} FROM {spec.table} t WHERE {where}"
            if order:
                sql += " " + order.format(ts=spec.ts)
            if limit:
                sql += " " + limit
            out.append(f"({sql})")
    return out


_ROW_COLUMNS = ("{src} AS src, t.id AS id, {ts} AS ts, {started} AS started, "
                "{finished} AS finished, {raw} AS raw, {fts} AS _fts")


def _encode(ts: Any, row_id: Any) -> str:
    return f"{_iso(ts)}|{_text(row_id)}"


def _key_of(item: Mapping[str, Any]) -> tuple[str, str]:
    return _text(item.get("created_at")), _text(item.get("id"))


# ── Labels: names, never raw ids ─────────────────────────────────────────────────────────────

_STEP_FINISHED = {"succeeded", "failed", "skipped", "cancelled", "canceled", "completed", "done",
                  "error", "timed_out"}


def _labels(connection: Connection, project_id: str,
            items: list[Mapping[str, Any]]) -> dict[str, str]:
    """The names of the runs, steps and work items on a page, one batched read per kind.

    A row reads `Ship it #182`, never `crun_8f2…`: the console shows a raw id only where no
    name exists."""
    cruns = sorted({_text(i.get("run_id")) for i in items if _text(i.get("run_id"))})
    wfrs = sorted({_text(i.get("workflow_run_id")) for i in items
                   if _text(i.get("workflow_run_id"))})
    acts = sorted({_text(_as_mapping(i.get("data")).get("act_id")) for i in items
                   if _text(_as_mapping(i.get("data")).get("act_id"))})
    labels: dict[str, str] = {}
    if cruns:
        for row in _fetch_rows(connection, "SELECT id, title FROM code_task_runs "
                               "WHERE id = ANY(:ids) AND project_id = :p",
                               {"ids": cruns, "p": project_id}):
            if _text(row.get("title")):
                labels[_text(row["id"])] = _text(row["title"])
    if wfrs:
        from .command_center import run_name, step_label

        for row in _fetch_rows(connection, """
                SELECT r.id, r.input, d.workflow_key, d.name AS workflow_name, v.spec,
                       (SELECT jsonb_object_agg(x.step_id, x.status) FROM (
                            SELECT DISTINCT ON (step_id) step_id, status
                            FROM workflow_step_runs
                            WHERE workflow_run_id = r.id AND project_id = r.project_id
                            ORDER BY step_id, created_at DESC, iteration DESC) x) AS step_states
                FROM workflow_runs r
                JOIN workflow_definitions d ON d.id = r.workflow_definition_id
                LEFT JOIN workflow_versions v ON v.id = r.workflow_version_id
                WHERE r.id = ANY(:ids) AND r.project_id = :p""",
                {"ids": wfrs, "p": project_id}):
            run = {**row, "input": _as_mapping(row.get("input"))}
            try:
                labels[_text(row["id"])] = run_name(run)
            except Exception:  # noqa: BLE001 — a label is never worth failing a read
                pass
            # `<wfr>/<step>@state`: whether the step has finished, so an empty step reads
            # "Nothing logged" when it is done and "…yet" only while it can still log.
            for step_id, status in _as_mapping(row.get("step_states")).items():
                labels[f"{_text(row['id'])}/{_text(step_id)}@state"] = (
                    "finished" if _text(status).lower() in _STEP_FINISHED else "open")
            spec = _as_mapping(row.get("spec"))
            for step in spec.get("steps") or []:
                if isinstance(step, Mapping) and _text(step.get("id")):
                    try:
                        labels[f"{_text(row['id'])}/{_text(step.get('id'))}"] = step_label(
                            step, _text(step.get("id")))
                    except Exception:  # noqa: BLE001
                        pass
    if acts:
        for row in _fetch_rows(connection, "SELECT act_id, label FROM agent_acts "
                               "WHERE act_id = ANY(:ids) AND project_id = :p",
                               {"ids": acts, "p": project_id}):
            if _text(row.get("label")):
                labels[_text(row["act_id"])] = _text(row["label"])
    return scrub(labels)


def _scope_probe(query: LogQuery) -> list[dict[str, Any]]:
    """The scope's own ids as label probes, so `run: Ship it #182` reads before any row does —
    looked up in the same batched reads as the page's ids, not in reads of their own."""
    probe: list[dict[str, Any]] = []
    if query.run_id:
        probe.append({"run_id": query.run_id})
    if query.workflow_run_id:
        probe.append({"workflow_run_id": query.workflow_run_id})
    if query.act:
        probe.append({"data": {"act_id": query.act}})
    return probe


def _subject_ids(connection: Connection, query: "LogQuery") -> list[str]:
    """Everything `subject=` stands for, so every stream matches the same thing.

    A sibling screen links here with the id it holds. The Infra deploy drawer holds a deploy
    REF (`delivery:deprun_…`, `stack:sdep_…`), which no row is keyed by — "Open full log"
    matched nothing. The ref is read once, by infra_view's one reader, and a deploy brings the
    records it is made of: its job (`job.*`), its approval (`decision.*`), its ChangeSet and its
    receipts (`resource_receipt.*`). Never the build: that is a different log."""
    from .infra_view import split_deploy_ref

    subject = _text(query.subject)
    ids: list[str] = [subject] if subject else []
    parsed = split_deploy_ref(subject)
    if parsed and parsed[1] not in ids:
        ids.append(parsed[1])
    if not ids or "deployment" in query.missing:
        return ids
    rows = _fetch_rows(connection, """
        SELECT d.job_id, d.decision_id, d.changeset_id, NULL::text AS reservation_id,
               d.release_receipt_ids AS receipts
        FROM deployment_runs d WHERE d.project_id = :p AND d.id = ANY(:ids)
        UNION ALL
        SELECT x.job_id, x.decision_id, x.changeset_id, x.reservation_id, NULL::jsonb
        FROM stack_deployments x WHERE x.project_id = :p AND x.id = ANY(:ids)""",
                       {"p": query.project_id, "ids": ids})
    for row in rows:
        linked = [row.get("job_id"), row.get("decision_id"), row.get("changeset_id"),
                  row.get("reservation_id")]
        receipts = row.get("receipts")
        if isinstance(receipts, str):
            try:
                receipts = json.loads(receipts)
            except ValueError:
                receipts = []
        linked += list(receipts) if isinstance(receipts, list) else []
        ids += [_text(v) for v in linked if _text(v) and _text(v) not in ids]
    return ids


#: Databases already seen to hold every source table. A partial schema is re-checked each time
#: (a table can be created later); a complete one never loses a table at runtime.
_COMPLETE: set[str] = set()


def _missing_sources(connection: Connection) -> set[str]:
    """Streams whose table is absent. One UNION ALL over ten tables would fail as a whole on a
    missing relation, where the old per-source loaders skipped just that source."""
    engine = getattr(connection, "engine", None)
    key = str(getattr(engine, "url", "")) if engine is not None else ""
    if not key or key in _COMPLETE:
        return set()
    tables = sorted({t for spec in activity_sql.SOURCES.values() for t in spec.tables()})
    rows = _fetch_rows(connection,
                       "SELECT name, to_regclass(name) IS NOT NULL AS present "
                       "FROM unnest(CAST(:tables AS text[])) AS name", {"tables": tables})
    absent = {_text(r["name"]) for r in rows if not r.get("present")}
    if not absent and rows:
        _COMPLETE.add(key)
    return {name for name, spec in activity_sql.SOURCES.items()
            if any(t in absent for t in spec.tables())}


class ActivityLogService:
    """Project-scoped, read-only union of everything BOSS logged — as queries, not scans."""

    def __init__(self, settings: Any = None):
        self.settings = settings

    def list_activity(self, connection: Connection, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.query(connection, project_id, **kwargs)

    def list(self, connection: Connection, project_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.query(connection, project_id, **kwargs)

    # ── the page ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def prepare(connection: Connection, project_id: str, **params: Any) -> LogQuery:
        query = LogQuery.build(project_id, **params)
        query.missing = _missing_sources(connection)
        if query.subject:
            query.subjects = _subject_ids(connection, query)
        if query.step_id:
            query.work = sorted(_step_work_ids(connection, query.project_id,
                                               query.workflow_run_id, query.step_id))
        return query

    @staticmethod
    def query(
        connection: Connection,
        project_id: str,
        *,
        sort: Optional[str] = "created_at",
        order: Optional[str] = "desc",
        limit: Optional[int] = DEFAULT_LIMIT,
        offset: Optional[int] = 0,
        cursor: Optional[str] = None,
        after: Optional[str] = None,
        total: Any = True,
        summary: Any = False,
        **params: Any,
    ) -> dict[str, Any]:
        """One page of the log, newest first, by keyset.

        Each source reads at most `offset + limit + 1` rows through its (project_id, time)
        index with every filter applied in SQL; one UNION ALL merges them. Nothing is loaded
        to be thrown away, so a filtered page costs what an unfiltered one does."""
        if _text(sort or "created_at").lower() != "created_at":
            raise ValueError(SORT_REFUSED)
        parsed_order = _normalize_order(order)
        parsed_limit = _normalize_limit(limit)
        parsed_offset = _normalize_offset(offset)
        if _text(cursor) and parsed_offset:
            raise ValueError("use cursor or offset, not both.")
        if _text(cursor) and _text(after):
            raise ValueError("use cursor (older) or after (newer), not both.")
        query = ActivityLogService.prepare(connection, project_id, **params)
        if str(summary).lower() in ("1", "true", "yes"):
            return ActivityLogService._summary(connection, query, limit=min(parsed_limit, 20))

        params_sql = _params(query)
        descending = parsed_order == "desc"
        keyset = ""
        if _text(cursor):
            stamp, row_id = _decode_cursor(_text(cursor))
            params_sql["c_ts"] = _parse_time(stamp, "cursor")
            params_sql["c_id"] = row_id
            op, edge = ("<", "<=") if descending else (">", ">=")
            keyset = f"{{ts}} {edge} :c_ts AND ({{ts}}, t.id COLLATE \"C\") {op} (:c_ts, :c_id)"
        newer = bool(_text(after))
        if newer:
            stamp, row_id = _decode_cursor(_text(after))
            params_sql["a_ts"] = _parse_time(stamp, "after")
            params_sql["a_id"] = row_id
            keyset = "{ts} >= :a_ts AND ({ts}, t.id COLLATE \"C\") > (:a_ts, :a_id)"
        direction = "DESC" if (descending or newer) else "ASC"
        fetch = parsed_offset + parsed_limit + 1
        branches = _branches(
            query, _ROW_COLUMNS, extra=keyset,
            order=f'ORDER BY {{ts}} {direction}, t.id COLLATE "C" {direction}',
            limit=f"LIMIT {fetch}")
        rows: list[dict[str, Any]] = []
        if branches:
            rows = _fetch_rows(
                connection,
                "SELECT * FROM (" + " UNION ALL ".join(branches) + ") u"
                + f' ORDER BY ts {direction}, id COLLATE "C" {direction} LIMIT {fetch}',
                params_sql)
        more = len(rows) > parsed_offset + parsed_limit
        window_rows = rows[parsed_offset:parsed_offset + parsed_limit]
        items = [_build(row) for row in window_rows]
        if query.q:
            # SQL narrowed (full-text where indexed, a plain-text haystack elsewhere); this
            # keeps the search meaning what an operator thinks it means. Full-text matches are
            # not re-checked: they matched on content `_matches_q` cannot see.
            items = [item for item, row in zip(items, window_rows)
                     if row.get("_fts") or _matches_q(item, query.q)]
        next_cursor = (_encode(window_rows[-1]["ts"], window_rows[-1]["id"])
                       if window_rows and more and not newer else None)
        newest_cursor = (_encode(rows[0]["ts"], rows[0]["id"]) if rows and descending else None)
        result: dict[str, Any] = {
            "items": items,
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_cursor": next_cursor,
            "newest_cursor": newest_cursor,
            "window": query.window(),
            "labels": _labels(connection, query.project_id, items + _scope_probe(query)),
        }
        if newer:
            # More new rows than one page: the reader should reload rather than splice.
            result["gap"] = more
        if str(total).lower() not in ("0", "false", "no"):
            counted, exact = ActivityLogService._count(connection, query)
            result["total"] = counted
            result["total_is_exact"] = exact
            if not exact:
                result["truncated_note"] = (
                    "Counted by the search index and a plain-text match; a few counted rows "
                    "may not contain the exact text.")
        return result

    @staticmethod
    def _count(connection: Connection, query: LogQuery) -> tuple[int, bool]:
        branches = _branches(query, "count(*) AS n")
        if not branches:
            return 0, True
        rows = _fetch_rows(connection,
                           "SELECT coalesce(sum(n), 0) AS n FROM (" + " UNION ALL ".join(branches)
                           + ") c", _params(query))
        exact = not query.q or all(activity_sql.SOURCES[s].fts for s in query.selected())
        return int(rows[0]["n"] if rows else 0), exact

    @staticmethod
    def _summary(connection: Connection, query: LogQuery, *, limit: int = 5) -> dict[str, Any]:
        """The step panel's read: the newest non-verbose rows, errors first, and how many
        errors and warnings there are — one bounded statement, not a page and two counts."""
        if not query.levels:
            query.hide_verbose = True
        branches = _branches(query, _ROW_COLUMNS + ", {level} AS lvl")
        rows: list[dict[str, Any]] = []
        if branches:
            # ONE statement: the rows, the error/warn counts, and the step's model calls (how
            # many, their money — a subscription call is not money — and the newest model).
            rows = _fetch_rows(connection, f"""
                WITH u AS ({' UNION ALL '.join(branches)}),
                     s AS (
                       SELECT count(*) FILTER (WHERE lvl = 'error') AS n_error,
                              count(*) FILTER (WHERE lvl = 'warn') AS n_warn,
                              count(*) FILTER (WHERE src = 'llm_call') AS n_calls,
                              coalesce(sum(CASE WHEN src = 'llm_call'
                                    AND NOT coalesce((raw->>'included_in_subscription')::boolean, false)
                                    THEN (raw->>'cost_usd_micros')::bigint ELSE 0 END), 0) AS money,
                              (SELECT raw->>'model' FROM u WHERE src = 'llm_call'
                                 AND coalesce(raw->>'model', '') <> ''
                                 ORDER BY ts DESC LIMIT 1) AS model
                       FROM u)
                SELECT u.*, s.* FROM s LEFT JOIN u ON true
                ORDER BY CASE u.lvl WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                         u.ts DESC, u.id COLLATE "C" DESC
                LIMIT {int(limit)}""", _params(query))
        stats = rows[0] if rows else {}
        rows = [r for r in rows if r.get("id")]
        items = [_build(row) for row in rows]
        return {
            "items": items, "limit": limit, "offset": 0, "next_cursor": None,
            "counts": {"error": int(stats.get("n_error") or 0),
                       "warn": int(stats.get("n_warn") or 0)},
            "calls": {"count": int(stats.get("n_calls") or 0),
                      "cost_usd": round(int(stats.get("money") or 0) / 1_000_000, 4),
                      "model": _text(stats.get("model"))},
            "window": query.window(),
            "labels": _labels(connection, query.project_id, items + _scope_probe(query)),
        }

    # ── the counts: histogram and facets ───────────────────────────────────────────────────

    @staticmethod
    def histogram(connection: Connection, project_id: str, *,
                  now: Optional[datetime] = None, **params: Any) -> dict[str, Any]:
        """Rows per time bucket, stacked by level — the pulse above the list. Real counts."""
        now = now or datetime.now(timezone.utc)
        if not _text(params.get("range")) and not params.get("since"):
            params = {**params, "range": "24h"}
        query = ActivityLogService.prepare(connection, project_id, now=now, **params)
        start = query.since or (now - RANGES["24h"])
        end = query.until or now
        span = max(end - start, timedelta(seconds=1))
        bucket = next((seconds for limit, seconds in _BUCKETS if span <= limit), 21600)
        if span > RANGES["30d"]:
            bucket = max(bucket, int(span.total_seconds() // 120))
        branches = _branches(query, "{ts} AS ts, {level} AS lvl")
        buckets: dict[str, dict[str, int]] = {}
        if branches:
            params_sql = {**_params(query), "bucket": timedelta(seconds=bucket),
                          "origin": datetime(2000, 1, 1, tzinfo=timezone.utc)}
            for row in _fetch_rows(connection, f"""
                    SELECT date_bin(:bucket, ts, :origin) AS b, lvl, count(*) AS n
                    FROM ({' UNION ALL '.join(branches)}) u GROUP BY 1, 2 ORDER BY 1""",
                    params_sql):
                key = _iso(row["b"])
                entry = buckets.setdefault(key, {name: 0 for name in LEVELS})
                level = _text(row["lvl"]) if _text(row["lvl"]) in LEVELS else "info"
                entry[level] += int(row["n"] or 0)
        # DENSE: every bucket of the window, empty ones as zeros, so a quiet hour reads as a
        # quiet hour and the bars line up with the clock rather than with the data.
        origin = datetime(2000, 1, 1, tzinfo=timezone.utc)
        step = timedelta(seconds=bucket)
        cursor_t = origin + ((start - origin) // step) * step
        ordered = []
        while cursor_t <= end:
            key = _iso(cursor_t)
            ordered.append({"t": key, **buckets.pop(key, {name: 0 for name in LEVELS})})
            cursor_t += step
        ordered.extend({"t": key, **counts} for key, counts in sorted(buckets.items()))
        total = sum(sum(b[name] for name in LEVELS) for b in ordered)
        return {"project_id": query.project_id, "buckets": ordered, "bucket_seconds": bucket,
                "from": _iso(start), "to": _iso(end), "total": total,
                "total_is_exact": not query.q or all(
                    activity_sql.SOURCES[s].fts for s in query.selected()),
                "window": query.window()}

    @staticmethod
    def facets(connection: Connection, project_id: str, **params: Any) -> dict[str, Any]:
        """How many rows match per LEVEL, STREAM, AGENT, RUN, PROCESS and STATUS — counted in
        SQL over the window, each facet ignoring its own selection (so a chip says how many
        rows selecting it would show). Never counted from a page."""
        query = ActivityLogService.prepare(connection, project_id, **params)
        base_query = LogQuery(**{**query.__dict__, "levels": [], "exclude_levels": [],
                                 "hide_verbose": False, "status": "", "agents": [],
                                 "exclude_agents": [], "service": "", "sources": [],
                                 "exclude_sources": [], "kinds": []})
        branches = _branches(
            base_query,
            "{src} AS src, {level} AS lvl, {status} AS st, {agent} AS agent, "
            "{service} AS svc, {run_key} AS run_key, {kind} AS kd",
            sources=list(SOURCES))
        levels = {name: 0 for name in LEVELS}
        sources = {name: 0 for name in SOURCES}
        agents: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        services: dict[str, int] = {}
        kinds: list[dict[str, Any]] = []
        statuses = {name: 0 for name in STATUSES}
        total = 0
        if branches:
            p = {**_params(query), "selected": query.selected()}
            level_pred = ("lvl = ANY(:levels)" if query.levels
                          else ("lvl <> 'verbose'" if query.hide_verbose else "true"))
            if query.exclude_levels:
                level_pred += " AND lvl <> ALL(:exclude_levels)"
            preds = {
                "level": level_pred,
                "source": "src = ANY(:selected)",
                "agent": ("agent = ANY(:agents)" if query.agents else "true")
                + (" AND coalesce(agent, '') <> ALL(:exclude_agents)"
                   if query.exclude_agents else ""),
                "service": "svc = :service" if query.service else "true",
                "status": "st = :status" if query.status else "true",
                # A kind token is a real kind or a stream name (`kind:job`), as in _where.
                "kind": ("(kd = ANY(:kinds) OR src = ANY(:kinds))" if query.kinds else "true"),
            }

            def but(name: str) -> str:
                return " AND ".join(v for k, v in preds.items() if k != name)

            everything = " AND ".join(preds.values())
            sql = f"""
                WITH base AS MATERIALIZED ({' UNION ALL '.join(branches)})
                SELECT 'level' AS f, lvl AS k, count(*) AS n FROM base WHERE {but('level')}
                    GROUP BY lvl
                UNION ALL SELECT 'source', src, count(*) FROM base WHERE {but('source')}
                    GROUP BY src
                UNION ALL SELECT 'status', st, count(*) FROM base WHERE {but('status')}
                    GROUP BY st
                UNION ALL SELECT 'service', svc, count(*) FROM base WHERE {but('service')}
                    AND coalesce(svc, '') <> '' GROUP BY svc
                UNION ALL (SELECT 'agent', agent, count(*) FROM base WHERE {but('agent')}
                    AND coalesce(agent, '') <> '' GROUP BY agent ORDER BY 3 DESC, 2 LIMIT 41)
                UNION ALL (SELECT 'kind', kd, count(*) FROM base WHERE {but('kind')}
                    AND coalesce(kd, '') <> '' GROUP BY kd ORDER BY 3 DESC, 2 LIMIT 40)
                UNION ALL (SELECT 'run', run_key, count(*) FROM base WHERE {everything}
                    AND coalesce(run_key, '') <> '' GROUP BY run_key ORDER BY 3 DESC, 2 LIMIT 9)
                UNION ALL SELECT 'total', '', count(*) FROM base WHERE {everything}"""
            for row in _fetch_rows(connection, sql, p):
                f, k, n = _text(row["f"]), _text(row["k"]), int(row["n"] or 0)
                if f == "level":
                    levels[k if k in LEVELS else "info"] = levels.get(
                        k if k in LEVELS else "info", 0) + n
                elif f == "source":
                    sources[k] = n
                elif f == "status":
                    statuses[k] = n
                elif f == "service":
                    services[k] = n
                elif f == "agent":
                    agents.append({"role_key": k, "n": n})
                elif f == "run":
                    runs.append({"id": k, "n": n})
                elif f == "kind":
                    kinds.append({"kind": k, "n": n})
                elif f == "total":
                    total = n
        labels = _labels(connection, query.project_id,
                         [{"run_id": r["id"]} if r["id"].startswith("crun_")
                          else {"workflow_run_id": r["id"]} for r in runs])
        for run in runs:
            run["label"] = labels.get(run["id"], "")
        exact = not query.q or all(activity_sql.SOURCES[s].fts for s in SOURCES)
        return {
            "project_id": query.project_id,
            "levels": levels, "sources": sources, "statuses": statuses, "services": services,
            "agents": agents[:40], "agents_more": max(0, len(agents) - 40),
            # The real kinds in the window, for `kind:` autocomplete (D13), most common first.
            "kinds": kinds,
            "runs": runs[:8], "runs_more": len(runs) > 8,
            "total": total, "levels_exact": exact, "sources_exact": exact, "exact": exact,
            "window": query.window(),
        }

    @staticmethod
    def kinds(connection: Connection, project_id: str, *, prefix: str = "", limit: int = 20,
              **params: Any) -> dict[str, Any]:
        """`kind:` completion BY PREFIX over the whole window (kind:deploy → deploy.production),
        counted in SQL, most common first. The facets read keeps only the top 40 kinds; a kind
        past them was unreachable by typing. The current filter applies, except any kind."""
        query = ActivityLogService.prepare(connection, project_id, **params)
        query = LogQuery(**{**query.__dict__, "kinds": []})
        want = _text(prefix).lower()
        branches = _branches(query, "{kind} AS kd", sources=list(SOURCES))
        found: list[dict[str, Any]] = []
        if branches:
            sql = (f"SELECT kd, count(*) AS n FROM ({' UNION ALL '.join(branches)}) u "
                   "WHERE coalesce(kd, '') <> '' AND lower(kd) LIKE :prefix ESCAPE '\\' "
                   "GROUP BY kd ORDER BY 2 DESC, 1 LIMIT :limit")
            escaped = want.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            p = {**_params(query), "prefix": f"{escaped}%", "limit": max(1, min(int(limit), 50))}
            found = [{"kind": _text(r["kd"]), "n": int(r["n"] or 0)} for r in _fetch_rows(connection, sql, p)]
        return {"project_id": query.project_id, "prefix": want, "kinds": found,
                "window": query.window()}

    # ── one row, and the rows around it ────────────────────────────────────────────────────

    @staticmethod
    def row(connection: Connection, project_id: str, row_id: str) -> Optional[dict[str, Any]]:
        """One line by id, from whichever stream holds it (a primary-key probe per source)."""
        project = _text(project_id)
        if not project or not _text(row_id):
            raise ValueError("project_id and id are required.")
        branches = []
        missing = _missing_sources(connection)
        for name, spec in activity_sql.SOURCES.items():
            if name in missing:
                continue
            columns = _ROW_COLUMNS.format(src=f"'{name}'", ts=spec.ts, raw=spec.raw,
                                          started=spec.started, finished=spec.finished,
                                          fts="false")
            for scope in spec.scopes:
                branches.append(f"(SELECT {columns} FROM {spec.table} t "
                                f"WHERE t.id = :row_id AND {scope})")
        rows = _fetch_rows(connection, " UNION ALL ".join(branches) + " LIMIT 1",
                           {"row_id": _text(row_id), "project_id": project})
        if not rows:
            return None
        item = _build(rows[0])
        return {"item": item, "labels": _labels(connection, project, [item])}

    @staticmethod
    def context(connection: Connection, project_id: str, row_id: str, *,
                before: int = 20, after: int = 20) -> Optional[dict[str, Any]]:
        """The lines just before and after one line, in the same run (or the same stream and
        process when it has no run) — log context, the way Loki and Grafana show it."""
        found = ActivityLogService.row(connection, project_id, row_id)
        if found is None:
            return None
        anchor = found["item"]
        scope: dict[str, Any] = {}
        if anchor.get("workflow_run_id"):
            scope["workflow_run_id"] = anchor["workflow_run_id"]
        elif anchor.get("run_id"):
            scope["run_id"] = anchor["run_id"]
        else:
            scope["source"] = anchor["source"]
            if anchor.get("service"):
                scope["service"] = anchor["service"]
        cursor = _encode(anchor["created_at"], anchor["id"])
        older = ActivityLogService.query(connection, project_id, cursor=cursor,
                                         limit=max(1, min(int(before), 100)), total=False,
                                         **scope)
        newer = ActivityLogService.query(connection, project_id, cursor=cursor, order="asc",
                                         limit=max(1, min(int(after), 100)), total=False,
                                         **scope)
        items = list(reversed(newer["items"])) + [anchor] + older["items"]
        return {"anchor": anchor["id"], "items": items, "scope": scope,
                "labels": {**older["labels"], **newer["labels"], **found["labels"]}}

    # ── export ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def export_params(project_id: str, **params: Any) -> dict[str, Any]:
        """The filter with its window bounded to 30 days (the newest 30 when none was given)."""
        query = LogQuery.build(project_id, **params)
        until = query.until or datetime.now(timezone.utc)
        if query.since is None or until - query.since > timedelta(days=EXPORT_MAX_DAYS):
            return {**params, "since": _iso(until - timedelta(days=EXPORT_MAX_DAYS)),
                    "range": None}
        return params

    @staticmethod
    def export(connection: Connection, project_id: str, *, max_rows: int = EXPORT_MAX_ROWS,
               **params: Any) -> Iterator[dict[str, Any]]:
        """Every row the current filter matches, newest first, redacted, bounded."""
        params = ActivityLogService.export_params(project_id, **params)
        cursor = None
        sent = 0
        while sent < max_rows:
            page = ActivityLogService.query(connection, project_id, cursor=cursor,
                                            limit=MAX_LIMIT, total=False, **params)
            for item in page["items"]:
                if sent >= max_rows:
                    return
                yield item
                sent += 1
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ── the ETag: what changed, without reading a page ─────────────────────────────────────

    @staticmethod
    def signature(connection: Connection, query: LogQuery, *, extra: str = "") -> str:
        """A strong ETag for a read: the filters plus the newest row of every stream that could
        contribute (one index probe each, one statement). Unchanged → the client's copy stands."""
        probes = []
        for name in query.selected():
            spec = activity_sql.SOURCES[name]
            parts = " , ".join(f"(SELECT max({spec.newest}) FROM {spec.table} t WHERE {scope})"
                               for scope in spec.scopes)
            probes.append(f'greatest({parts}) AS "{name}"' if len(spec.scopes) > 1
                          else f'{parts} AS "{name}"')
        newest = _fetch_rows(connection, "SELECT " + ", ".join(probes),
                             {"project_id": query.project_id}) if probes else [{}]
        stamp = json.dumps({k: _iso(v) for k, v in (newest[0] if newest else {}).items()},
                           sort_keys=True)
        digest = hashlib.sha256(f"{query.key()}|{extra}|{stamp}".encode()).hexdigest()[:32]
        return f'"{digest}"'
