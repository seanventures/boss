"""QL-1 (BOSSAPP-795): Dedicated per-host Quick Lane queue with memory-aware admission.

- Reserved quick-lane slot: at most ``MAX_CONCURRENT_PER_HOST`` (1) quick job runs per host,
  completely independent of full-build (``code.task``) slots so a quick fix runs immediately
  while long builds occupy the normal build slots, and vice versa.
- Priority: person-asked quick jobs always run ahead of system-started ones (both in
  ``quick_lane_runs`` claiming and in ``jobs.priority``).
- Memory-aware admission: a quick job starts only when the host has at least ``required_mb``
  available; otherwise it stays queued in ``waiting_memory`` with an explicit stated reason
  and an emitted ``quick_lane.waiting_memory`` event.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import emit_event
from ..jobs import JOB_PRIORITY_BACKGROUND, JOB_PRIORITY_REQUESTED

#: Dedicated worker job kind for Quick Lane runs.
JOB_KIND = "quick.fix"

#: Dedicated workload class so Quick Lane never competes with full-build capacity counters.
WORKLOAD_CLASS = "quick_lane"

#: One quick-lane job at a time per host (QUICK_LANE.md §3.2).
MAX_CONCURRENT_PER_HOST = 1

#: Default sandbox memory floor (MB) required before admitting a quick job on a host.
DEFAULT_REQUIRED_MEMORY_MB = 1536

#: Active statuses occupying a host's reserved quick-lane slot.
ACTIVE_SLOT_STATUSES = ("running", "validating")


@dataclass(frozen=True)
class AdmitDecision:
    """Result of checking whether a host can admit a quick-lane job right now."""

    admitted: bool
    reason: str
    detail: str
    host_id: str
    available_memory_mb: Optional[int] = None
    required_memory_mb: int = DEFAULT_REQUIRED_MEMORY_MB
    active_on_host: int = 0


def priority_for_requester(requested_by_person: bool) -> int:
    """Person-asked quick jobs come strictly before system-started ones."""
    return JOB_PRIORITY_REQUESTED if requested_by_person else JOB_PRIORITY_BACKGROUND


def read_host_available_memory_mb() -> Optional[int]:
    """Read currently available host memory in MiB without shelling out.

    Checks Linux ``/proc/meminfo`` first, then POSIX ``sysconf`` fallback when available.
    Returns ``None`` if unavailable on this platform.
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            content = fh.read()
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", content, re.MULTILINE)
        if match:
            return int(match.group(1)) // 1024
    except OSError:
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        avail_pages = os.sysconf("SC_AVPHYS_PAGES")
        if isinstance(page_size, int) and isinstance(avail_pages, int) and page_size > 0:
            return (page_size * avail_pages) // (1024 * 1024)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def check_host_memory(
    *,
    host_id: str,
    available_memory_mb: Optional[int] = None,
    required_memory_mb: int = DEFAULT_REQUIRED_MEMORY_MB,
    memory_probe: Optional[Callable[[], Optional[int]]] = None,
) -> AdmitDecision:
    """Memory-aware admission check for a single host.

    Fail-open only when ``available_memory_mb`` and the probe both return ``None`` (e.g. macOS
    dev host without ``/proc/meminfo``); when memory is measurable and below ``required_memory_mb``,
    refuses admission with a plain stated reason so the job waits instead of OOM-killing the host.
    """
    avail = available_memory_mb
    if avail is None and memory_probe is not None:
        avail = memory_probe()
    if avail is None:
        avail = read_host_available_memory_mb()
    if avail is not None and avail < required_memory_mb:
        return AdmitDecision(
            admitted=False,
            reason="insufficient_host_memory",
            detail=(
                f"Host {host_id} has {avail} MB available memory; "
                f"quick sandbox requires {required_memory_mb} MB."
            ),
            host_id=host_id,
            available_memory_mb=avail,
            required_memory_mb=required_memory_mb,
        )
    return AdmitDecision(
        admitted=True,
        reason="ok",
        detail=(
            f"Host {host_id} has {avail} MB available (>= {required_memory_mb} MB)."
            if avail is not None
            else f"Host {host_id} memory probe unavailable; slot admitted."
        ),
        host_id=host_id,
        available_memory_mb=avail,
        required_memory_mb=required_memory_mb,
    )


def host_active_quick_count(connection: Connection, host_id: str) -> int:
    """Count active quick-lane jobs currently occupying ``host_id``'s reserved slot.

    Deliberately counts ONLY ``quick_lane_runs`` (never ``code_task_runs``) so full builds
    running on the same host never block the reserved quick-lane slot.
    """
    count = connection.execute(
        text(
            "SELECT COUNT(*) FROM quick_lane_runs "
            "WHERE host_id = :host_id AND status IN ('running', 'validating')"
        ),
        {"host_id": host_id},
    ).scalar()
    return int(count or 0)


def admit_quick_job(
    connection: Connection,
    *,
    run_id: str,
    project_id: str,
    host_id: str,
    available_memory_mb: Optional[int] = None,
    required_memory_mb: int = DEFAULT_REQUIRED_MEMORY_MB,
    memory_probe: Optional[Callable[[], Optional[int]]] = None,
    actor: str = "worker",
) -> AdmitDecision:
    """Check host slot + memory for ``run_id``; record ``waiting_memory``/reason if deferred."""
    active = host_active_quick_count(connection, host_id)
    if active >= MAX_CONCURRENT_PER_HOST:
        decision = AdmitDecision(
            admitted=False,
            reason="host_quick_slot_busy",
            detail=(
                f"Host {host_id} already has {active}/{MAX_CONCURRENT_PER_HOST} "
                "quick-lane job running; waiting for the reserved quick slot."
            ),
            host_id=host_id,
            available_memory_mb=available_memory_mb,
            required_memory_mb=required_memory_mb,
            active_on_host=active,
        )
        connection.execute(
            text(
                "UPDATE quick_lane_runs "
                "SET waiting_reason = :reason, updated_at = now() "
                "WHERE id = :id AND project_id = :project_id AND status IN ('queued', 'waiting_memory')"
            ),
            {"id": run_id, "project_id": project_id, "reason": decision.detail},
        )
        emit_event(
            connection,
            project_id,
            "quick_lane.waiting_slot",
            actor,
            "quick_lane_run",
            run_id,
            {"host_id": host_id, "active": active, "reason": decision.reason},
        )
        return decision

    mem = check_host_memory(
        host_id=host_id,
        available_memory_mb=available_memory_mb,
        required_memory_mb=required_memory_mb,
        memory_probe=memory_probe,
    )
    if not mem.admitted:
        connection.execute(
            text(
                "UPDATE quick_lane_runs "
                "SET status = 'waiting_memory', phase = 'waiting_memory', "
                "    waiting_reason = :reason, updated_at = now() "
                "WHERE id = :id AND project_id = :project_id AND status IN ('queued', 'waiting_memory')"
            ),
            {"id": run_id, "project_id": project_id, "reason": mem.detail},
        )
        emit_event(
            connection,
            project_id,
            "quick_lane.waiting_memory",
            actor,
            "quick_lane_run",
            run_id,
            {
                "host_id": host_id,
                "available_memory_mb": mem.available_memory_mb,
                "required_memory_mb": mem.required_memory_mb,
                "reason": mem.reason,
                "detail": mem.detail,
            },
        )
        return mem

    connection.execute(
        text(
            "UPDATE quick_lane_runs "
            "SET status = 'running', phase = 'context', host_id = :host_id, "
            "    waiting_reason = NULL, updated_at = now() "
            "WHERE id = :id AND project_id = :project_id"
        ),
        {"id": run_id, "project_id": project_id, "host_id": host_id},
    )
    emit_event(
        connection,
        project_id,
        "quick_lane.admitted",
        actor,
        "quick_lane_run",
        run_id,
        {
            "host_id": host_id,
            "available_memory_mb": mem.available_memory_mb,
            "required_memory_mb": mem.required_memory_mb,
        },
    )
    return mem


def claim_next_quick_job(
    connection: Connection,
    *,
    host_id: str,
    project_id: Optional[str] = None,
    available_memory_mb: Optional[int] = None,
    required_memory_mb: int = DEFAULT_REQUIRED_MEMORY_MB,
    memory_probe: Optional[Callable[[], Optional[int]]] = None,
    actor: str = "worker",
) -> tuple[Optional[dict[str, Any]], AdmitDecision]:
    """Claim the highest-priority waiting quick-lane run for ``host_id``.

    Ordering:
      1. Person-asked jobs (``requested_by_person = TRUE``) before system-started jobs.
      2. Oldest first (``created_at ASC``).

    Returns ``(claimed_run_or_None, AdmitDecision)``.
    """
    active = host_active_quick_count(connection, host_id)
    if active >= MAX_CONCURRENT_PER_HOST:
        return None, AdmitDecision(
            admitted=False,
            reason="host_quick_slot_busy",
            detail=f"Host {host_id} quick slot is occupied ({active}/{MAX_CONCURRENT_PER_HOST}).",
            host_id=host_id,
            available_memory_mb=available_memory_mb,
            required_memory_mb=required_memory_mb,
            active_on_host=active,
        )

    where = "status IN ('queued', 'waiting_memory', 'ci_reopened')"
    params: dict[str, Any] = {}
    if project_id:
        where += " AND project_id = :project_id"
        params["project_id"] = project_id

    row = connection.execute(
        text(
            "SELECT id, project_id, title, status, source, requested_by_person, actor, spec "
            f"FROM quick_lane_runs WHERE {where} "
            "ORDER BY requested_by_person DESC, created_at ASC "
            "LIMIT 1 FOR UPDATE SKIP LOCKED"
        ),
        params,
    ).mappings().first()
    if row is None:
        return None, AdmitDecision(
            admitted=False,
            reason="queue_empty",
            detail="No quick-lane jobs waiting.",
            host_id=host_id,
            available_memory_mb=available_memory_mb,
            required_memory_mb=required_memory_mb,
        )

    decision = admit_quick_job(
        connection,
        run_id=str(row["id"]),
        project_id=str(row["project_id"]),
        host_id=host_id,
        available_memory_mb=available_memory_mb,
        required_memory_mb=required_memory_mb,
        memory_probe=memory_probe,
        actor=actor,
    )
    if not decision.admitted:
        return None, decision

    claimed = dict(row)
    claimed["status"] = "running"
    claimed["host_id"] = host_id
    return claimed, decision
