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

SOURCES = ("event", "process_step", "job", "deadletter", "receipt")
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
    needle = q.casefold()
    haystack = " ".join(
        (
            _text(item.get("kind")),
            _text(item.get("actor")),
            _text(item.get("entity_type")),
            _text(item.get("entity_id")),
            _text(item.get("title")),
        )
    ).casefold()
    return needle in haystack


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
) -> dict[str, Any]:
    classified = _classify(classify_row)
    return {
        "id": id,
        "source": source,
        "kind": kind,
        "status": classified["status"],
        "title": classified["title"],
        "actor": actor,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "stage": classified["stage"],
        "created_at": _iso(created_at),
        "data": _redact_data(data),
    }


def _load_events(
    connection: Connection,
    project_id: str,
    *,
    since: Any,
    until: Any,
    cap: int,
) -> list[dict[str, Any]]:
    extra, params = _time_clauses(since, until)
    rows = _fetch_rows(
        connection,
        f"""
        SELECT id, event_type, actor, entity_type, entity_id, data, created_at
        FROM events
        WHERE project_id = :project_id{extra}
        ORDER BY created_at DESC
        LIMIT :cap
        """,
        {"project_id": project_id, "cap": cap, **params},
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        event_type = _text(row.get("event_type"))
        data = _as_mapping(row.get("data"))
        items.append(
            _item(
                id=_text(row.get("id")),
                source="event",
                kind=event_type,
                actor=_text(row.get("actor")),
                entity_type=_text(row.get("entity_type")),
                entity_id=_text(row.get("entity_id")),
                created_at=row.get("created_at"),
                data=data,
                classify_row={"event_type": event_type, "kind": event_type},
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


def _count_all(connection: Connection, project_id: str, *, since: Any, until: Any) -> int:
    """Total activity rows for the project, across every source.

    Counted rather than inferred from the page. `total` was `len(items)` back when every row
    was loaded; with the sources bounded, that number would be the page size wearing a total's
    name -- the same mistake as the console's "Verified today: 100", which was reporting its
    own fetch limit as a fact about the world.
    """
    extra, params = _time_clauses(since, until)
    total = 0
    for table in ("events", "agent_process_steps", "jobs", "deadletters", "receipts"):
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
)


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
        )
        exact = (
            not filters_active
            and parsed_sort == "created_at"
            and parsed_order == "desc"
        )
        cap = (parsed_offset + parsed_limit) if exact else FILTERED_SCAN_CAP

        items: list[dict[str, Any]] = []
        source_counts: list[int] = []
        for loader in _SOURCE_LOADERS:
            loaded = loader(connection, project, since=since, until=until, cap=cap)
            source_counts.append(len(loaded))
            items.extend(loaded)
        # A source that returned exactly its cap probably had more to give.
        capped = not exact and any(count >= cap for count in source_counts)

        if needle:
            items = [item for item in items if _matches_q(item, needle)]
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

        page = items[parsed_offset : parsed_offset + parsed_limit]

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
