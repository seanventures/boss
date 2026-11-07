"""What happened to this BOSS install, in facts with timestamps: its upgrades and its restarts.

WHY THIS EXISTS (boss-dogfood 2026-11-07)
-----------------------------------------
The system doctor said "Updates restarted a build 2 times in an hour". The Infra Steward's squad
had to settle "was that our upgrade, or a crash loop (OOM, a watchdog)?" and could not: BOSS kept
no record of which commit it ran when, and a container has no docker socket (and must not be
given one). So the squad asked a person for "host:install log access" — twice — on a day the
laptop was upgraded a dozen times.

WHAT IT RECORDS
---------------
Every start of a BOSS service (api, worker) writes one `boss_starts` row: the running commit
(BOSS_SOURCE_SHA, stamped into the image at build), the image's build time (BOSS_BUILT_AT), the
container's hostname (a container RECREATED by an upgrade gets a new one; one restarted in place by
Docker's restart policy keeps it), the cgroup's OOM-kill count at boot, and the commit the same
service ran before. A start whose commit differs from the one before it IS an upgrade (from → to,
when, by which install method).

WHAT IT ANSWERS (`restarts`, the read tool `install_restarts`)
--------------------------------------------------------------
Each start in a window, classified from those facts:
  upgrade   the commit changed: an install or upgrade ran (upgrade.sh, the self-fix installer);
  rebuild   same commit, a newer image: a reinstall rebuilt it;
  recreate  same image, a new container: `docker compose up` replaced it (an operator, an
            installer step);
  restart   same container started again: Docker's restart policy after the process died —
            the crash-loop signature (an OOM kill shows in `oom_kills`);
  unknown   a start from before this record existed (from the activity log; no commit).
plus the builds those starts interrupted (a build re-queued after an upgrade carries
`retried_after_upgrade`). One call settles "the restart at 19:44 was our upgrade to 2b66afb0".
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

#: The activity-log line every service start writes (log_capture): starts from before boss_starts.
_LEGACY_START = "activity-log capture installed for %"


def _stamp(name: str) -> Optional[str]:
    value = (os.environ.get(name) or "").strip()
    return None if not value or value == "unknown" else value


def _oom_kills() -> Optional[int]:
    try:
        from .self_observability import memory_snapshot

        value = memory_snapshot().get("oom_kills")
        return int(value) if value is not None else None
    except Exception:  # noqa: BLE001 — a host without cgroup v2 counters has no count
        return None


def record_start(engine: Any, service: str) -> Optional[dict[str, Any]]:
    """Write this service's start. Returns the row (with `upgraded` when the commit changed).
    Never raises: a start that cannot be recorded never blocks boot."""
    commit = _stamp("BOSS_SOURCE_SHA")
    row = {"id": f"bst_{uuid.uuid4().hex[:16]}", "service": service, "commit": commit,
           "built_at": _stamp("BOSS_BUILT_AT"), "host": socket.gethostname()[:120],
           "oom_kills": _oom_kills(), "install_method": _stamp("BOSS_INSTALL_METHOD")}
    try:
        with engine.begin() as c:
            before = c.execute(text(
                "SELECT source_commit FROM boss_starts WHERE service = :s "
                "ORDER BY started_at DESC LIMIT 1"), {"s": service}).scalar()
            row["previous_commit"] = before
            c.execute(text(
                "INSERT INTO boss_starts (id, service, source_commit, previous_commit, built_at, "
                "host, oom_kills, install_method) VALUES (:id, :service, :commit, "
                ":previous_commit, :built_at, :host, :oom_kills, :install_method)"), row)
        row["upgraded"] = bool(before and commit and before != commit)
        return row
    except Exception:  # noqa: BLE001
        return None


def _when(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def classify(start: Mapping[str, Any], before: Optional[Mapping[str, Any]]) -> str:
    """upgrade | rebuild | recreate | restart | first (see the module docstring)."""
    if before is None:
        return "first"
    if start.get("commit") and before.get("commit") and start["commit"] != before["commit"]:
        return "upgrade"
    if start.get("built_at") and before.get("built_at") and \
            start["built_at"] != before["built_at"]:
        return "rebuild"
    if start.get("host") and before.get("host") and start["host"] != before["host"]:
        return "recreate"
    return "restart"


_SAYS = {"upgrade": "BOSS was upgraded (the running commit changed)",
         "rebuild": "BOSS was reinstalled: same commit, a newly built image",
         "recreate": "the container was replaced (compose up): same image",
         "restart": "the same container started again: the process died and Docker restarted "
                    "it (crash-loop signature)",
         "first": "the first start on record for this service",
         "unknown": "a start from before BOSS recorded its commit (activity log only)"}


def restarts(connection: Any, *, hours: float = 24.0,
             now: Optional[datetime] = None) -> dict[str, Any]:
    """Every start of BOSS's services in the last `hours`, classified, with the upgrades and
    the builds those starts interrupted. Read-only."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=max(0.1, min(float(hours or 24), 24 * 14)))
    rows = [dict(r) for r in connection.execute(text(
        "SELECT service, source_commit AS src, previous_commit, built_at, host, oom_kills, "
        "install_method, "
        "started_at FROM boss_starts WHERE started_at >= :s - interval '7 days' "
        "ORDER BY service, started_at"), {"s": since}).mappings()]
    for r in rows:
        r["commit"] = r.pop("src")
    starts: list[dict[str, Any]] = []
    last: dict[str, dict[str, Any]] = {}
    for r in rows:
        kind = classify(r, last.get(r["service"]))
        if kind == "first" and r.get("previous_commit") and r["previous_commit"] != r["commit"]:
            kind = "upgrade"
        last[r["service"]] = r
        if r["started_at"] < since:
            continue
        starts.append({"service": r["service"], "at": _when(r["started_at"]), "kind": kind,
                       "says": _SAYS[kind], "commit": (r["commit"] or "")[:12] or None,
                       "from_commit": (r["previous_commit"] or "")[:12] or None,
                       "built_at": r["built_at"], "container": r["host"],
                       "oom_kills_at_boot": r["oom_kills"], "by": r["install_method"]})
    recorded_from = min((r["started_at"] for r in rows), default=None)
    legacy = [{"service": r["service"], "at": _when(r["created_at"]), "kind": "unknown",
               "says": _SAYS["unknown"]} for r in connection.execute(text(
                   "SELECT service, created_at FROM app_logs WHERE logger = "
                   "'boss_engine.services.log_capture' AND message LIKE :m "
                   "AND created_at >= :s AND (CAST(:r AS TIMESTAMPTZ) IS NULL "
                   "OR created_at < :r) ORDER BY created_at"),
                   {"m": _LEGACY_START, "s": since, "r": recorded_from}).mappings()]
    interrupted = [{"build": r["id"], "title": (r["title"] or "")[:80],
                    "requeued_after_upgrade_at": r["at"]} for r in connection.execute(text(
                        "SELECT id, title, finalize_ctx->>'retried_after_upgrade' AS at "
                        "FROM code_task_runs WHERE finalize_ctx ? 'retried_after_upgrade' "
                        "AND updated_at >= :s ORDER BY updated_at DESC LIMIT 20"),
                        {"s": since}).mappings()]
    every = sorted(legacy + starts, key=lambda s: s["at"] or "")
    upgrades = [{"at": s["at"], "service": s["service"], "from": s["from_commit"],
                 "to": s["commit"], "by": s["by"]} for s in starts if s["kind"] == "upgrade"]
    counts: dict[str, int] = {}
    for s in every:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    return {"window_hours": round((now - since).total_seconds() / 3600, 1),
            "running": {"commit": _stamp("BOSS_SOURCE_SHA"), "built_at": _stamp("BOSS_BUILT_AT"),
                        "oom_kills_now": _oom_kills()},
            "counts": counts, "upgrades": upgrades, "starts": every,
            "builds_interrupted": interrupted,
            "how_to_read": ("A restart at the time of an `upgrade` (or rebuild/recreate) start was "
                            "BOSS's own install, not a crash. `restart` starts — the same "
                            "container again — and a rising OOM count are the crash-loop "
                            "signature.")}


__all__ = ["record_start", "restarts", "classify"]
