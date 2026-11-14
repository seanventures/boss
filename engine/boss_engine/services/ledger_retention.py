"""Retention for the run ledger's own table, `run_events` (AP-P0; HARNESS_KERNEL_DESIGN.md D6).

The ledger is on by default now (kernel/ledger.enabled), so `run_events` grows with every run.
It is append-only (a trigger refuses UPDATE and DELETE) and range-partitioned by month on
`recorded_at`, so retention never rewrites a row: it DROPS whole monthly partitions whose
every row is older than the window, and it makes next months' partitions ahead of time (the
schema made them only at boot, so a host that ran past them wrote into the default partition).

Rows that did land in `run_events_default` past the window are removed with the purge switch
the trigger honours (SET LOCAL boss.ledger_purge = 'on'), in bounded batches.

The window is metadata retention (D6: 400 days). `llm_calls` and `tool_call_audit` keep their
own retention; this module never touches them. A floor of 30 days holds whatever is asked.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

ENV_DAYS = "BOSS_LEDGER_RETENTION_DAYS"
DEFAULT_DAYS = 400
MINIMUM_DAYS = 30
MONTHS_AHEAD = 2
BATCH = 5_000

_PART = re.compile(r"^run_events_(\d{4})_(\d{2})$")


def retention_days() -> int:
    raw = (os.environ.get(ENV_DAYS) or "").strip()
    try:
        asked = int(raw) if raw else DEFAULT_DAYS
    except ValueError:
        return DEFAULT_DAYS
    return max(MINIMUM_DAYS, asked)


def _month_end(year: int, month: int) -> date:
    """The exclusive upper bound of a monthly partition."""
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def expired_partitions(names: list, *, today: date, days: int) -> list:
    """The monthly partitions whose upper bound is at or before today - days."""
    out = []
    for name in names:
        m = _PART.match(name)
        if not m:
            continue
        end = _month_end(int(m.group(1)), int(m.group(2)))
        if (today - end).days >= days:
            out.append(name)
    return sorted(out)


def prune(connection: Any, *, days: Optional[int] = None,
          now: Optional[datetime] = None) -> dict:
    """Make the next partitions, drop expired months, trim the default partition. Never raises:
    housekeeping that fails a worker loop is worse than housekeeping that skips an hour."""
    window = max(MINIMUM_DAYS, int(days) if days else retention_days())
    today = (now or datetime.now(timezone.utc)).date()
    out: dict = {"days": window, "made": 0, "dropped": [], "default_rows": 0}
    try:
        if connection.execute(text("SELECT to_regclass('run_events')")).scalar() is None:
            return out
        out["made"] = int(connection.execute(
            text("SELECT run_events_ensure_partitions(:n)"), {"n": MONTHS_AHEAD}).scalar() or 0)
        names = [r[0] for r in connection.execute(text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'run_events'::regclass")).all()]
        for name in expired_partitions(names, today=today, days=window):
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
            out["dropped"].append(name)
        connection.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
        while True:
            got = connection.execute(text(
                "DELETE FROM run_events_default WHERE ctid IN (SELECT ctid FROM "
                "run_events_default WHERE recorded_at < CAST(:today AS date) - :d "
                "LIMIT :b)"), {"today": today.isoformat(), "d": window, "b": BATCH})
            n = int(got.rowcount or 0)
            out["default_rows"] += n
            if n < BATCH:
                break
        connection.execute(text("SET LOCAL boss.ledger_purge = 'off'"))
    except Exception:  # noqa: BLE001
        log.debug("ledger_retention_failed", exc_info=True)
        out["error"] = True
    return out


__all__ = ["DEFAULT_DAYS", "MINIMUM_DAYS", "expired_partitions", "prune", "retention_days"]
