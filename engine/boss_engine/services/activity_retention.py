"""How long the log keeps what it captured.

Per-turn capture writes far more rows than anything before it — a single build can produce
several hundred, and BOSS runs builds continuously. Unbounded, the transcript table would
outgrow every other table in the system within weeks.

The floor is deliberate and low in the code rather than in configuration: an operator debugging
a customer's build needs the last month, and a retention setting that can be turned down to a
day is a retention setting that will be, on the afternoon someone is chasing disk space. Thirty
days is the minimum this module will honour, whatever it is asked for.

This deletes ROWS in bounded batches. It does not touch files, directories, or any record of
what BOSS built — code, runs, PRs and receipts are not log data and are not in scope here.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

#: Never keep less than this, whatever the environment says.
MINIMUM_DAYS = 30

#: Rows removed per statement. A single unbounded DELETE over a month of transcripts takes a
#: long lock on a table the console reads; batches keep the log usable while it is pruned.
BATCH = 20_000

#: Only the high-volume, purely diagnostic tables. `events` is NOT here: it is the audit
#: stream, other features read it, and quietly aging out a customer's audit trail from a
#: routine housekeeping job is not a decision this module gets to make.
#: `app_logs` joins the transcript here for the same reason it exists: it is the highest-volume
#: table in the console — one row per interesting log line across the API and every worker — and
#: a log nobody ages out becomes the thing that fills the disk.
PRUNABLE = ("agent_turns", "app_logs")


def retention_days() -> int:
    raw = (os.environ.get("BOSS_ACTIVITY_RETENTION_DAYS") or "").strip()
    try:
        requested = int(raw) if raw else MINIMUM_DAYS
    except ValueError:
        return MINIMUM_DAYS
    return max(MINIMUM_DAYS, requested)


def prune(connection: Connection, *, days: int | None = None) -> dict[str, Any]:
    """Delete log rows older than the retention window. Returns what it removed.

    Reports per table rather than a single number so an operator can see WHICH log grew, and
    never raises: housekeeping that can fail a worker loop is worse than housekeeping that
    skips a cycle.
    """
    window = max(MINIMUM_DAYS, int(days) if days else retention_days())
    removed: dict[str, int] = {}
    for table in PRUNABLE:
        total = 0
        try:
            while True:
                result = connection.execute(
                    text(
                        f"""
                        DELETE FROM {table}
                        WHERE id IN (
                            SELECT id FROM {table}
                            WHERE created_at < now() - make_interval(days => :days)
                            LIMIT :batch
                        )
                        """
                    ),
                    {"days": window, "batch": BATCH},
                )
                count = int(result.rowcount or 0)
                total += count
                if count < BATCH:
                    break
        except Exception as exc:  # noqa: BLE001 — a missing table is not an outage
            log.warning("activity retention skipped %s (non-fatal): %s", table, exc)
        if total:
            removed[table] = total
    return {"retention_days": window, "removed": removed,
            "total_removed": sum(removed.values())}
