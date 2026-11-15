"""THE SYSTEM-SERVICES REGISTRY (AP-P3, design decision D7; AGENT_PLATFORM §4.4 "System services
stay outside").

The deterministic daemons — the system doctor, the reaper, stop_watch, the backup, extension and
ticket schedulers, the login refreshers, the worker's sweeps — are code, not models, so they are
NOT agents and never become bots. They are listed HERE, in one table, so "always-on" has one list
and one health view: each registers when it starts (system_threads.start_all, the worker) and the
ones with a clock beat on every pass. The Health screen (A5) reads this list beside the bots.
"""
from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.system_services")

#: Every service BOSS runs that is NOT an agent: key -> (name, kind, runs_in, interval seconds).
#: An interval means it beats; a service whose last beat is older than three intervals is stale.
KNOWN: dict[str, tuple[str, str, str, Optional[int]]] = {
    "system_doctor": ("Doctor", "thread", "api", 15),
    "reaper": ("Reaper", "thread", "api", None),
    "stop_watch": ("Stop watch", "thread", "api", None),
    "ledger": ("Ownership ledger", "thread", "api", None),
    "drain_on_sigterm": ("Drain on deploy", "thread", "api", None),
    "claude_login": ("Claude login refresher", "thread", "api", None),
    "codex_login": ("Codex login refresher", "thread", "api", None),
    "backup_scheduler": ("Backups", "scheduler", "api", None),
    "skill_sources": ("Skill source sync", "scheduler", "api", None),
    "extension_scheduler": ("Extension scheduler", "scheduler", "api", None),
    "ticket_sync": ("Ticket sync", "scheduler", "api", None),
    "intake_processor": ("Event intake", "thread", "api", 2),
    "reflection": ("Reflection schedule", "scheduler", "api", None),
    "bot_clock": ("Bot clock", "worker_tick", "worker", 60),
    "worker_sweeps": ("Worker sweeps", "worker_tick", "worker", 5),
}


def _run(engine_or_connection: Any, sql: str, params: dict) -> None:
    if hasattr(engine_or_connection, "connect") and not hasattr(engine_or_connection, "commit"):
        with engine_or_connection.begin() as c:
            c.execute(text(sql), params)
    else:
        engine_or_connection.execute(text(sql), params)


def register(engine_or_connection: Any, key: str, *, started: bool = True,
             details: Optional[dict] = None) -> None:
    """A service started (or was turned off). Never raises: a registry that can stop a service
    from starting is worse than none."""
    name, kind, runs_in, interval = KNOWN.get(key, (key.replace("_", " ").capitalize(),
                                                    "thread", "api", None))
    try:
        _run(engine_or_connection, """
            INSERT INTO system_services (key, name, kind, runs_in, interval_seconds, status,
                   started_at, last_beat_at, details, host, updated_at)
            VALUES (:k, :n, :kind, :in, :iv, :s, now(), now(), CAST(:d AS JSONB), :h, now())
            ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                   runs_in = EXCLUDED.runs_in, interval_seconds = EXCLUDED.interval_seconds,
                   status = EXCLUDED.status, started_at = now(), last_beat_at = now(),
                   details = system_services.details || EXCLUDED.details, host = EXCLUDED.host,
                   updated_at = now()""",
             {"k": key, "n": name, "kind": kind, "in": runs_in, "iv": interval,
              "s": "running" if started else "off", "d": json.dumps(details or {}, default=str),
              "h": socket.gethostname()[:120]})
    except Exception:  # noqa: BLE001
        _log.debug("system service %s not registered", key, exc_info=True)


def beat(engine_or_connection: Any, key: str, *, ok: bool = True, error: Optional[str] = None,
         details: Optional[dict] = None) -> None:
    """One pass of a clocked service: alive, and whether the pass went well."""
    if key not in KNOWN:
        return
    try:
        _run(engine_or_connection, """
            INSERT INTO system_services (key, name, kind, runs_in, interval_seconds, status,
                   started_at, last_beat_at, last_ok_at, last_error, details, updated_at)
            VALUES (:k, :n, :kind, :in, :iv, :s, now(), now(),
                    CASE WHEN :ok THEN now() END, :e, CAST(:d AS JSONB), now())
            ON CONFLICT (key) DO UPDATE SET last_beat_at = now(),
                   last_ok_at = CASE WHEN :ok THEN now() ELSE system_services.last_ok_at END,
                   last_error = CASE WHEN :ok THEN system_services.last_error ELSE :e END,
                   status = CASE WHEN :ok THEN 'running' ELSE 'failing' END,
                   details = system_services.details || CAST(:d AS JSONB), updated_at = now()""",
             {"k": key, "n": KNOWN[key][0], "kind": KNOWN[key][1], "in": KNOWN[key][2],
              "iv": KNOWN[key][3], "s": "running" if ok else "failing", "ok": bool(ok),
              "e": (error or "")[:500] or None, "d": json.dumps(details or {}, default=str)})
    except Exception:  # noqa: BLE001
        _log.debug("system service %s beat not recorded", key, exc_info=True)


def listing(connection: Any) -> list[dict]:
    """Every registered service with its state in words: running, stale (no beat for three
    intervals), failing (its last pass failed), off."""
    now = datetime.now(timezone.utc)
    out = []
    for r in connection.execute(text(
            "SELECT * FROM system_services ORDER BY runs_in, name")).mappings().all():
        row = dict(r)
        state = row["status"]
        last = row.get("last_beat_at")
        interval = row.get("interval_seconds")
        if state == "running" and interval and last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() > 3 * int(interval):
                state = "stale"
        out.append({"key": row["key"], "name": row["name"], "kind": row["kind"],
                    "runs_in": row["runs_in"], "state": state,
                    "ok": state == "running",
                    "interval_seconds": interval, "last_beat_at": _iso(row.get("last_beat_at")),
                    "last_ok_at": _iso(row.get("last_ok_at")), "last_error": row.get("last_error"),
                    "details": row.get("details") or {}, "host": row.get("host")})
    return out


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


__all__ = ["KNOWN", "beat", "listing", "register"]
