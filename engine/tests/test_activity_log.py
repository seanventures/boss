"""Contract tests for ActivityLogService (BOSS-379 slice 3).

Seam: the public list/query on `engine/boss_engine/services/activity_log.py`.
SoR remains `events` plus read-only projections of process steps, jobs,
deadletters, and receipts. Secret material must never be returned.

Expected public API (from `.ai/swarm-2026-08-14-activity.md`):

    ActivityLogService().list_activity(
        connection, project_id, *,
        q=None, kind=None, status=None, actor=None, entity_type=None,
        since=None, until=None, sort="created_at", order="desc",
        limit=50, offset=0,
    ) -> {"items": [ActivityItem, ...], ...}  or  list[ActivityItem]

    ActivityItem: id, source, kind, status, title, actor, entity_type,
                  entity_id, stage, created_at, data (redacted)

    source ∈ {event, process_step, job, deadletter, receipt}
    status ∈ {ok, failed, waiting, stuck, info}
    sort   ∈ {created_at, status, kind}

If `activity_log.py` is missing (slice 2), these tests skip/xfail with that
reason instead of erroring on import.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.exc import ProgrammingError

ITEM_KEYS = {
    "id",
    "source",
    "kind",
    "status",
    "title",
    "actor",
    "entity_type",
    "entity_id",
    "stage",
    "created_at",
    "data",
}
SOURCES = {"event", "process_step", "job", "deadletter", "receipt"}
STATUSES = {"ok", "failed", "waiting", "stuck", "info"}
OPTIONAL_TABLES = (
    "jobs",
    "deadletters",
    "receipts",
    "agent_process_steps",
    "process_steps",
)

PROJECT_A = "proj_activity_a"
PROJECT_B = "proj_activity_b"

T_OLD = datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)
T_MID = datetime(2026, 8, 14, 11, 0, 0, tzinfo=timezone.utc)
T_NEW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

# Distinctive secret literals — independent of redact() implementation.
SECRET_TOKEN = "tok_live_should_never_surface"
SECRET_EMAIL = "ops@example.com"
SECRET_AWS = "AKIAIOSFODNN7EXAMPLE"


# ---------------------------------------------------------------------------
# Import / public-API resolution
# ---------------------------------------------------------------------------


def _load_module():
    return pytest.importorskip(
        "boss_engine.services.activity_log",
        reason=(
            "activity_log.py missing (BOSS-379 slice 2); "
            "contract tests wait on ActivityLogService"
        ),
    )


def _make_service(mod: Any) -> Any:
    cls = getattr(mod, "ActivityLogService", None)
    if cls is not None:
        try:
            return cls()
        except TypeError:
            from boss_engine.config import Settings

            return cls(Settings())
    return mod


def _list_fn(service: Any):
    for name in ("list_activity", "list", "query", "search", "fetch"):
        fn = getattr(service, name, None)
        if callable(fn):
            return fn
    pytest.xfail(
        "activity_log.py imported but has no list_activity/list/query "
        "(BOSS-379 slice 2)"
    )


def _call_list(service: Any, connection: Any, project_id: str, **kwargs: Any) -> Any:
    fn = _list_fn(service)
    errors: list[TypeError] = []
    for call in (
        lambda: fn(connection, project_id, **kwargs),
        lambda: fn(connection, project_id=project_id, **kwargs),
        lambda: fn(connection=connection, project_id=project_id, **kwargs),
    ):
        try:
            return call()
        except TypeError as exc:
            errors.append(exc)
    raise errors[-1]


def _items(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        raw = result.get("items")
        if raw is None:
            raw = result.get("activity")
        if raw is None:
            pytest.fail(f"list result dict has no items/activity: {sorted(result)}")
        result = raw
    rows = list(result)
    out: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "_mapping"):
            out.append(dict(row._mapping))
        elif isinstance(row, dict):
            out.append(dict(row))
        else:
            out.append({key: getattr(row, key) for key in ITEM_KEYS if hasattr(row, key)})
    return out


@pytest.fixture
def service():
    return _make_service(_load_module())


# ---------------------------------------------------------------------------
# Lightweight in-memory connection (no Postgres)
# ---------------------------------------------------------------------------


class _Row(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def _mapping(self) -> dict[str, Any]:
        return dict(self)


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = [_Row(row) for row in rows]

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[_Row]:
        return list(self._rows)

    def fetchall(self) -> list[_Row]:
        return list(self._rows)

    def first(self) -> _Row | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


def _mentions(sql: str, table: str) -> bool:
    return re.search(rf"\b{re.escape(table)}\b", sql, re.IGNORECASE) is not None


def _undefined_table(table: str) -> ProgrammingError:
    orig = Exception(f'relation "{table}" does not exist')
    orig.pgcode = "42P01"  # type: ignore[attr-defined]
    orig.sqlstate = "42P01"  # type: ignore[attr-defined]
    return ProgrammingError(f"SELECT 1 FROM {table}", {}, orig)


class FakeActivityConn:
    """Table-aware execute() double.

    Returns raw source rows for any mentioned table. Applies ``project_id``
    only when the caller binds it. Honors a simple ``ORDER BY`` / ``LIMIT``
    so SQL-side sort/page still exercises the service. Does **not** apply
    status/q filters — those are ActivityLogService behavior.
    """

    def __init__(self, *, missing: set[str] | None = None):
        self.tables: dict[str, list[dict[str, Any]]] = {
            "events": [],
            "jobs": [],
            "deadletters": [],
            "receipts": [],
            "agent_process_steps": [],
            "process_steps": [],
        }
        self.missing = set(missing or ())
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def add(self, table: str, row: dict[str, Any]) -> None:
        self.tables.setdefault(table, []).append(dict(row))

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        self.statements.append((sql, params))
        for table in sorted(self.missing, key=len, reverse=True):
            if _mentions(sql, table):
                raise _undefined_table(table)

        mentioned = [
            table
            for table in (
                "agent_process_steps",
                "process_steps",
                "deadletters",
                "receipts",
                "events",
                "jobs",
            )
            if _mentions(sql, table)
        ]
        rows: list[dict[str, Any]] = []
        for table in mentioned:
            rows.extend(self.tables.get(table, []))

        project_id = params.get("project_id") or params.get("pid") or params.get("p")
        if project_id:
            rows = [row for row in rows if row.get("project_id") == project_id]

        order = re.search(
            r"order\s+by\s+(?:[\w]+\.)?(\w+)\s*(asc|desc)?",
            sql,
            re.IGNORECASE,
        )
        if order:
            key = order.group(1)
            reverse = (order.group(2) or "asc").lower() == "desc"
            rows = sorted(rows, key=lambda row: row.get(key) or "", reverse=reverse)
        elif params.get("sort") in {"created_at", "status", "kind"}:
            reverse = str(params.get("order") or "asc").lower() == "desc"
            rows = sorted(
                rows, key=lambda row: row.get(params["sort"]) or "", reverse=reverse
            )

        # The service counts rows to report an honest `total` now that the source queries are
        # bounded; the double has to answer that shape or every total reads as zero.
        if re.search(r"count\s*\(\s*\*\s*\)", sql, re.IGNORECASE):
            return _Result([{"n": len(rows)}])

        limit = params.get("limit")
        # `cap` is the per-source bound the service pushes into SQL. Honouring it here is what
        # makes "the page is exact" and "the scan was truncated" testable at all.
        cap = params.get("cap")
        offset = int(params.get("offset") or 0)
        if limit is not None:
            rows = rows[offset : offset + int(limit)]
        elif offset:
            rows = rows[offset:]
        if cap is not None:
            rows = rows[: int(cap)]
        return _Result(rows)


def _event(
    *,
    event_id: str,
    project_id: str,
    event_type: str,
    created_at: datetime,
    actor: str = "ceo",
    entity_type: str = "job",
    entity_id: str = "job_1",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "project_id": project_id,
        "event_type": event_type,
        "actor": actor,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "data": {} if data is None else data,
        "created_at": created_at,
    }


def _seed_timeline(conn: FakeActivityConn) -> FakeActivityConn:
    """Three project-A events with unambiguous classify() names.

    Insertion order is fail → budget → lease so it matches neither
    created_at desc/asc nor kind asc. A service that forgets to sort fails.
    """
    conn.add(
        "events",
        _event(
            event_id="evt_fail",
            project_id=PROJECT_A,
            event_type="job.failed",
            created_at=T_MID,
            entity_id="job_fail",
            data={"reason": "sandbox exploded"},
        ),
    )
    conn.add(
        "events",
        _event(
            event_id="evt_budget",
            project_id=PROJECT_A,
            event_type="budget.reserved",
            created_at=T_OLD,
            entity_type="reservation",
            entity_id="rsv_1",
            data={"note": "reserved compute"},
        ),
    )
    conn.add(
        "events",
        _event(
            event_id="evt_lease",
            project_id=PROJECT_A,
            event_type="job.leased",
            created_at=T_NEW,
            entity_id="job_wait",
            data={},
        ),
    )
    conn.add(
        "events",
        _event(
            event_id="evt_other",
            project_id=PROJECT_B,
            event_type="job.failed",
            created_at=T_NEW,
            entity_id="job_other",
            data={"note": "other tenant"},
        ),
    )
    return conn


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in items]


def _assert_item_shape(item: dict[str, Any]) -> None:
    missing = ITEM_KEYS - set(item)
    assert not missing, f"activity item missing {sorted(missing)}: {item}"
    assert item["source"] in SOURCES
    assert item["status"] in STATUSES
    assert isinstance(item["title"], str)
    assert isinstance(item["kind"], str)
    assert isinstance(item["actor"], str)
    assert isinstance(item["entity_type"], str)
    assert isinstance(item["entity_id"], str)
    assert item["data"] is None or isinstance(item["data"], dict)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_filter_by_status_returns_only_failed(service):
    conn = _seed_timeline(FakeActivityConn())
    items = _items(_call_list(service, conn, PROJECT_A, status="failed"))

    assert _ids(items) == ["evt_fail"]
    assert items[0]["status"] == "failed"
    assert items[0]["title"] == "Job Failed"
    for item in items:
        _assert_item_shape(item)
        assert item["status"] == "failed"


def test_filter_by_q_matches_kind_or_title(service):
    conn = _seed_timeline(FakeActivityConn())
    items = _items(_call_list(service, conn, PROJECT_A, q="budget"))

    assert _ids(items) == ["evt_budget"]
    blob = json.dumps(items).lower()
    assert "budget" in blob
    assert "evt_fail" not in _ids(items)
    assert "evt_lease" not in _ids(items)


def test_sort_created_at_desc_and_kind_asc(service):
    conn = _seed_timeline(FakeActivityConn())

    newest_first = _items(
        _call_list(service, conn, PROJECT_A, sort="created_at", order="desc")
    )
    assert _ids(newest_first) == ["evt_lease", "evt_fail", "evt_budget"]

    oldest_first = _items(
        _call_list(service, conn, PROJECT_A, sort="created_at", order="asc")
    )
    assert _ids(oldest_first) == ["evt_budget", "evt_fail", "evt_lease"]

    by_kind = _items(_call_list(service, conn, PROJECT_A, sort="kind", order="asc"))
    # budget.reserved < job.failed < job.leased (and the same order as titles)
    assert _ids(by_kind) == ["evt_budget", "evt_fail", "evt_lease"]


def test_redacts_secret_material(service):
    conn = FakeActivityConn()
    conn.add(
        "events",
        _event(
            event_id="evt_secret",
            project_id=PROJECT_A,
            event_type="job.failed",
            created_at=T_MID,
            data={
                "token": SECRET_TOKEN,
                "secret": "super-secret",
                "password": "hunter2",
                "api_key": SECRET_AWS,
                "authorization": "Bearer 0123456789abcdef0123",
                "note": f"ping {SECRET_EMAIL}",
                "aws_key": SECRET_AWS,
            },
        ),
    )
    items = _items(_call_list(service, conn, PROJECT_A))
    assert _ids(items) == ["evt_secret"]
    _assert_item_shape(items[0])

    blob = json.dumps(items, default=str)
    assert SECRET_TOKEN not in blob
    assert SECRET_EMAIL not in blob
    assert SECRET_AWS not in blob
    assert "hunter2" not in blob
    assert "Bearer 0123456789abcdef0123" not in blob
    assert "super-secret" not in blob


def test_missing_optional_table_does_not_raise(service):
    conn = FakeActivityConn(missing=set(OPTIONAL_TABLES))
    conn.add(
        "events",
        _event(
            event_id="evt_keep",
            project_id=PROJECT_A,
            event_type="budget.reserved",
            created_at=T_OLD,
            data={},
        ),
    )
    conn.add(
        "jobs",
        {
            "id": "job_missing_table",
            "project_id": PROJECT_A,
            "kind": "code.task",
            "status": "failed",
            "payload": {},
            "created_at": T_MID,
        },
    )

    items = _items(_call_list(service, conn, PROJECT_A))
    assert _ids(items) == ["evt_keep"]
    assert all(item["id"] != "job_missing_table" for item in items)
    for item in items:
        _assert_item_shape(item)


def test_tenant_scope_excludes_other_project_events(service):
    conn = _seed_timeline(FakeActivityConn())
    items = _items(_call_list(service, conn, PROJECT_A))

    ids = _ids(items)
    assert "evt_other" not in ids
    assert set(ids) == {"evt_budget", "evt_fail", "evt_lease"}
    for item in items:
        _assert_item_shape(item)


def test_does_not_invent_success_and_empty_payload_stays_empty(service):
    conn = FakeActivityConn()
    conn.add(
        "events",
        _event(
            event_id="evt_info",
            project_id=PROJECT_A,
            event_type="budget.reserved",
            created_at=T_OLD,
            data={},
        ),
    )
    items = _items(_call_list(service, conn, PROJECT_A))
    assert _ids(items) == ["evt_info"]
    _assert_item_shape(items[0])
    assert items[0]["status"] == "info"
    assert items[0]["status"] != "ok"
    assert items[0]["data"] == {}


# --------------------------------------------------------------------------- #
# Bounded source queries
#
# Every source query was unbounded: five of them loaded 46,774 rows on the dogfood project to
# display fifty, decoding 33,646 JSONB event payloads in Python on the way. /v1/activity took
# 14.3 seconds while /v1/dashboard took 0.6. These pin the bound AND its honesty — a bound that
# quietly drops rows turns a slow endpoint into a lying one.
# --------------------------------------------------------------------------- #
from boss_engine.services.activity_log import (  # noqa: E402
    FILTERED_SCAN_CAP,
    ActivityLogService,
)


def _service() -> ActivityLogService:
    return ActivityLogService()


def _many_events(conn: FakeActivityConn, count: int, project: str = "proj_a") -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(count):
        conn.add("events", _event(
            event_id=f"ev_{i:05d}",
            project_id=project,
            event_type="job.succeeded",
            created_at=base + timedelta(seconds=i),
        ))


def _caps_used(conn: FakeActivityConn) -> list[int]:
    return [p["cap"] for sql, p in conn.statements if "cap" in p]


def test_an_unfiltered_page_does_not_load_the_whole_history():
    """The default console view. Nothing about the answer changes; only how much is read."""
    conn = FakeActivityConn()
    _many_events(conn, 500)
    out = _service().query(conn, "proj_a", limit=50, offset=0)

    assert len(out["items"]) == 50
    # Each source asked for exactly what the page could contain, and no more.
    assert _caps_used(conn) == [50] * 5


def test_the_bound_covers_the_offset_not_just_the_page():
    """Asking for rows 200-250 needs the newest 250 per source, not the newest 50 — a cap of
    `limit` alone would return an empty second page."""
    conn = FakeActivityConn()
    _many_events(conn, 500)
    out = _service().query(conn, "proj_a", limit=50, offset=200)
    assert _caps_used(conn) == [250] * 5
    assert len(out["items"]) == 50


def test_the_unfiltered_page_is_identical_to_the_unbounded_one():
    """The bound is only sound if it changes no answer. A source cannot contribute a row to the
    newest fifty of the union unless it is in its own newest fifty."""
    conn = FakeActivityConn()
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for i in range(120):
        conn.add("events", _event(event_id=f"ev_{i:03d}", project_id="proj_a",
                                  event_type="job.succeeded",
                                  created_at=base + timedelta(seconds=i)))
    bounded = _service().query(conn, "proj_a", limit=25)
    # The newest 25 by id, since ids ascend with created_at here. Naming the exact rows is the
    # point: "25 rows, sorted" would also pass if the bound picked the WRONG 25.
    assert [i["id"] for i in bounded["items"]] == [f"ev_{i:03d}" for i in range(119, 94, -1)]


def test_total_is_counted_not_taken_from_the_page():
    """`total` used to be len(items), which was only true because everything was loaded. With
    the sources bounded, returning that would report 50 for a project with 33,646 events —
    the same defect as the console's "Verified today: 100", which was its own page size."""
    conn = FakeActivityConn()
    _many_events(conn, 300)
    out = _service().query(conn, "proj_a", limit=50)
    assert out["total"] == 300
    assert out["total_is_exact"] is True
    assert len(out["items"]) == 50


def test_a_filtered_search_reaches_past_the_page():
    """A filter runs in Python over loaded rows, so bounding to `limit` would hide every match
    that is not in the newest fifty — reporting "no failures" for a project that has them."""
    conn = FakeActivityConn()
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    conn.add("events", _event(event_id="ev_old_fail", project_id="proj_a",
                              event_type="job.failed", created_at=base))
    for i in range(1, 200):
        conn.add("events", _event(event_id=f"ev_{i:03d}", project_id="proj_a",
                                  event_type="job.succeeded",
                                  created_at=base + timedelta(seconds=i)))
    out = _service().query(conn, "proj_a", status="failed", limit=50)
    assert [i["id"] for i in out["items"]] == ["ev_old_fail"]
    assert _caps_used(conn) == [FILTERED_SCAN_CAP] * 5


def test_a_truncated_search_says_so_rather_than_implying_completeness():
    """The cap is real and cannot be wished away, so it is declared. A search that silently
    stopped would be worse than a slow one: it answers confidently and wrongly."""
    conn = FakeActivityConn()
    _many_events(conn, FILTERED_SCAN_CAP + 10)
    out = _service().query(conn, "proj_a", q="job", limit=50)
    assert out["total_is_exact"] is False
    assert "truncated_note" in out
    assert str(FILTERED_SCAN_CAP) in out["truncated_note"]


def test_a_filtered_search_within_the_cap_is_still_exact():
    """Declaring truncation on every filtered search would train the operator to ignore it."""
    conn = FakeActivityConn()
    _many_events(conn, 100)
    out = _service().query(conn, "proj_a", q="job", limit=50)
    assert out["total_is_exact"] is True
    assert "truncated_note" not in out


def test_sorting_by_status_does_not_use_the_recency_bound():
    """Newest-N is only a valid bound for a newest-first sort. Ordering by status has no
    relationship to recency, so the exact bound would silently pick the wrong rows."""
    conn = FakeActivityConn()
    _many_events(conn, 300)
    _service().query(conn, "proj_a", sort="status", limit=50)
    assert _caps_used(conn) == [FILTERED_SCAN_CAP] * 5


def test_ascending_order_does_not_use_the_recency_bound():
    """Oldest-first wants the OTHER end of the table; newest-N would return the wrong page."""
    conn = FakeActivityConn()
    _many_events(conn, 300)
    _service().query(conn, "proj_a", order="asc", limit=50)
    assert _caps_used(conn) == [FILTERED_SCAN_CAP] * 5


def test_every_source_is_bounded():
    """One unbounded source is enough to keep the endpoint slow — jobs was missed on the first
    pass because its query had an extra WHERE clause and did not match the others."""
    conn = FakeActivityConn()
    _many_events(conn, 10)
    _service().query(conn, "proj_a", limit=5)
    bounded = [sql for sql, p in conn.statements if "cap" in p]
    for table in ("events", "agent_process_steps", "jobs", "deadletters", "receipts"):
        assert any(f"FROM {table}" in sql for sql in bounded), f"{table} is unbounded"
