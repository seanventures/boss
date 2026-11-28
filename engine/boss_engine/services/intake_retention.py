"""Monthly partitioning and retention for `intake_events` (P9c-3, BOSSAPP-1107; LLD §4.5, §14).

Manages Postgres declarative range partitions on `intake_events(received_at)`:
  * `ensure_monthly_partitions`: pre-creates `intake_events_YYYY_MM` partitions (current month +
    `months_ahead` future months), safely migrating any rows already sitting in
    `intake_events_default` for that month in the same transaction so zero rows are lost.
  * `sweep_retention`:
      1. Raw payload nulling after 14 days (`raw_expires_at < now`, terminal rows only): sets
         `raw = NULL` while preserving `raw_sha256` and the envelope. Replay after this boundary
         is cleanly refused (`raw body expired (retention)`).
      2. Row deletion after 30 days (`routed`/`ignored`/`filtered`/`shed`/`digest`) or 90 days
         (`dead`/`refused`) (`expires_at < now`, never touching active `received`/`processing`/
         `failed` rows), pruning matching `intake_event_dedupe` entries.
      3. Whole-partition drops (`DROP TABLE intake_events_YYYY_MM`) for completed months strictly
         older than `dead_days` (default 90 days) that hold zero active or unexpired rows, plus
         pruning `intake_event_dedupe` for that month's window.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

RAW_RETENTION_DAYS = 14
ENVELOPE_RETENTION_DAYS = 30
DEAD_RETENTION_DAYS = 90
_PARTITION_RE = re.compile(r"^intake_events_(\d{4})_(\d{2})$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Return `[start, end)` UTC bounds for `(year, month)`."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, (total % 12) + 1


def partition_name(year: int, month: int) -> str:
    name = f"intake_events_{year:04d}_{month:02d}"
    if not _PARTITION_RE.fullmatch(name):
        raise ValueError(f"invalid partition name: {name}")
    return name


def is_partitioned(connection: Connection) -> bool:
    relkind = connection.execute(text(
        "SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = 'intake_events'")).scalar()
    return str(relkind or "") == "p"


def list_monthly_partitions(connection: Connection) -> list[dict[str, Any]]:
    """Every `intake_events_YYYY_MM` child partition attached to `intake_events` in this schema."""
    rows = connection.execute(text("""
        SELECT c.relname
          FROM pg_inherits i
          JOIN pg_class p ON p.oid = i.inhparent
          JOIN pg_class c ON c.oid = i.inhrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = current_schema() AND p.relname = 'intake_events'
         ORDER BY c.relname""")).all()
    out: list[dict[str, Any]] = []
    for (name,) in rows:
        m = _PARTITION_RE.fullmatch(str(name))
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        start, end = month_bounds(year, month)
        out.append({"name": str(name), "year": year, "month": month,
                    "start": start, "end": end})
    return out


def ensure_monthly_partitions(connection: Connection, *, now: Optional[datetime] = None,
                              months_back: int = 0, months_ahead: int = 2) -> dict[str, list[str]]:
    """Create monthly range partitions for `[-months_back, +months_ahead]` around `now`.
    Safely relocates any rows already in `intake_events_default` for each new month so
    Postgres's default-partition overlap check never fails and zero rows are lost."""
    now = now or _now()
    if not is_partitioned(connection):
        return {"created": [], "existing": []}
    existing = {p["name"] for p in list_monthly_partitions(connection)}
    created: list[str] = []
    already: list[str] = []

    for offset in range(-int(months_back), int(months_ahead) + 1):
        y, m = shift_month(now.year, now.month, offset)
        name = partition_name(y, m)
        if name in existing:
            already.append(name)
            continue
        start, end = month_bounds(y, m)
        # If rows for this month landed in `intake_events_default`, stage & re-insert them.
        staged = int(connection.execute(text(
            "SELECT count(*) FROM intake_events_default "
            "WHERE received_at >= :s AND received_at < :e"),
            {"s": start, "e": end}).scalar() or 0)
        if staged:
            connection.execute(text("DROP TABLE IF EXISTS _boss_intake_part_stage"))
            connection.execute(text(
                "CREATE TEMP TABLE _boss_intake_part_stage ON COMMIT DROP AS "
                "SELECT * FROM intake_events_default "
                "WHERE received_at >= :s AND received_at < :e"),
                {"s": start, "e": end})
            connection.execute(text(
                "DELETE FROM intake_events_default "
                "WHERE received_at >= :s AND received_at < :e"),
                {"s": start, "e": end})
        connection.execute(text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF intake_events "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"))
        if staged:
            connection.execute(text("INSERT INTO intake_events SELECT * FROM _boss_intake_part_stage"))
            connection.execute(text("DROP TABLE IF EXISTS _boss_intake_part_stage"))
        created.append(name)
        existing.add(name)
    return {"created": created, "existing": already}


def drop_expired_partitions(connection: Connection, *, now: Optional[datetime] = None,
                            dead_days: int = DEAD_RETENTION_DAYS) -> list[str]:
    """Drop monthly partitions strictly older than `now - dead_days` once they contain zero
    active (`received`/`processing`/`failed`) and zero unexpired (`expires_at >= now`) rows.
    Also prunes `intake_event_dedupe` entries belonging to each dropped month."""
    now = now or _now()
    if not is_partitioned(connection):
        return []
    cutoff = now - timedelta(days=int(dead_days))
    dropped: list[str] = []
    for part in list_monthly_partitions(connection):
        if part["end"] > cutoff:
            continue
        name = part["name"]
        if not _PARTITION_RE.fullmatch(name):
            continue
        held = int(connection.execute(text(
            f"SELECT count(*) FROM {name} "
            "WHERE status IN ('received', 'processing', 'failed') "
            "   OR (expires_at IS NOT NULL AND expires_at >= :now)"),
            {"now": now}).scalar() or 0)
        if held > 0:
            continue
        connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
        connection.execute(text(
            "DELETE FROM intake_event_dedupe WHERE received_at >= :s AND received_at < :e"),
            {"s": part["start"], "e": part["end"]})
        dropped.append(name)
    return dropped


def sweep_retention(connection: Connection, *, now: Optional[datetime] = None,
                    dead_days: int = DEAD_RETENTION_DAYS,
                    manage_partitions: bool = True) -> dict[str, Any]:
    """Full intake retention pass (LLD §4.5, BOSSAPP-1107):
      1. Null `raw` past 14-day `raw_expires_at` on terminal rows (keeps `raw_sha256`).
      2. Delete terminal rows past `expires_at` (30d routed/ignored, 90d dead/refused) and prune
         their `intake_event_dedupe` entries.
      3. Ensure current/future monthly partitions and drop expired monthly partitions whose upper
         bound is strictly past `dead_days` with no active/unexpired rows."""
    now = now or _now()
    nulled = connection.execute(text(
        "UPDATE intake_events SET raw = NULL WHERE raw IS NOT NULL AND raw_expires_at < :now "
        "AND status NOT IN ('received', 'processing', 'failed')"), {"now": now}).rowcount
    deleted_ids = [str(r[0]) for r in connection.execute(text(
        "DELETE FROM intake_events WHERE expires_at < :now "
        "AND status NOT IN ('received', 'processing', 'failed') RETURNING id"),
        {"now": now}).all()]
    dedupe_pruned = 0
    if deleted_ids:
        dedupe_pruned = int(connection.execute(text(
            "DELETE FROM intake_event_dedupe WHERE event_id = ANY(:ids)"),
            {"ids": deleted_ids}).rowcount or 0)
    partitions_created: list[str] = []
    partitions_dropped: list[str] = []
    if manage_partitions and is_partitioned(connection):
        ensured = ensure_monthly_partitions(connection, now=now, months_back=0, months_ahead=2)
        partitions_created = ensured["created"]
        partitions_dropped = drop_expired_partitions(connection, now=now, dead_days=dead_days)
    return {
        "raw_nulled": int(nulled or 0),
        "deleted": len(deleted_ids),
        "dedupe_pruned": dedupe_pruned,
        "partitions_created": partitions_created,
        "partitions_dropped": partitions_dropped,
    }


__all__ = [
    "DEAD_RETENTION_DAYS",
    "ENVELOPE_RETENTION_DAYS",
    "RAW_RETENTION_DAYS",
    "drop_expired_partitions",
    "ensure_monthly_partitions",
    "is_partitioned",
    "list_monthly_partitions",
    "month_bounds",
    "partition_name",
    "shift_month",
    "sweep_retention",
]
