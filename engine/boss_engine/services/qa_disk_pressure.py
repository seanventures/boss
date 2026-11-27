"""DISK PRESSURE FOR A TEST (`boss qa disk-pressure`, the plan's ENT-REC-29..31), with nothing written
to disk and nothing left behind.

The low-disk cases need the storage watch to read warn, critical or floor. Filling a laptop's disk
to get there is the risk the plan warned about; a small dedicated filesystem cannot be made what
BOSS measures (its readers are fixed paths, and /var/lib/boss holds BOSS's own data). So the
thresholds move instead: just above the free space the host has now, so the next two readings
cross them and every BOSS-side behaviour follows — the warning, the cleanup, the hold, and the
resume when the move ends.

THE MOVE IS MARKED AND TEMPORARY. The threshold rule carries `_qa` (storage_rules.thresholds reads
only known keys, so the marker is inert): the level, who, until when, the rule's source before, and
per key the value before and the value set. It ends three ways, all through `restore`:
`boss qa disk-pressure off`, its expiry (24 h by default; the storage watch's tick restores it),
and key by key: a threshold a person edited since keeps their value (every save carries the mark
forward, storage_rules.upsert, so a console edit never strands the test's raised values). A restore puts back ONLY a key still at the value the test set,
under the source the rule had (a default stays a default, never pinned as an operator's value).

A threshold is only ever RAISED (`max(current, free + margin)`), the bands stay ordered
(warn > critical > floor: storage_rules.check_order), and a production install is refused.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from . import storage_rules

MARK = "_qa"
LEVELS = ("warn", "critical", "floor")
#: The keys each level moves, and how far above today's free % each lands.
MOVES = {"warn": {"warn_free_pct": 6.0},
         "critical": {"warn_free_pct": 6.0, "critical_free_pct": 4.0},
         "floor": {"warn_free_pct": 6.0, "critical_free_pct": 4.0, "floor_free_pct": 2.0}}
DEFAULT_HOURS = 24
MAX_HOURS = 72


class Refused(ValueError):
    """The move cannot be made; the message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(connection: Any, host_id: Optional[str]) -> Optional[dict]:
    """The threshold row that applies to the host (its own, else the install-wide one)."""
    storage_rules.ensure_defaults(connection)
    return connection.execute(text(
        "SELECT * FROM retention_rules WHERE kind = 'threshold' AND (host_id IS NULL OR "
        "host_id = :h) ORDER BY host_id NULLS LAST LIMIT 1"), {"h": host_id or ""}).mappings().first()


def _rule(row: Optional[dict]) -> dict:
    raw = (row or {}).get("rule") or {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def state(connection: Any, host_id: Optional[str] = None) -> Optional[dict]:
    """The test's mark on the thresholds now, or None: {level, until, by, moved: {key: [before,
    set]}}. What the storage card shows ("thresholds moved for a test until …")."""
    mark = _rule(_row(connection, host_id)).get(MARK)
    return dict(mark) if isinstance(mark, dict) else None


def apply(connection: Any, *, level: str, actor: str, hours: float = DEFAULT_HOURS,
          host_id: Optional[str] = None) -> dict:
    from . import production, storage_watch

    if level not in LEVELS:
        raise Refused(f"level is one of {', '.join(LEVELS)}")
    hours = float(hours or DEFAULT_HOURS)
    if not 0 < hours <= MAX_HOURS:
        raise Refused(f"a test move lasts at most {MAX_HOURS} hours")
    verdict = production.resolve_install()
    if verdict.prod:
        raise Refused(f"this BOSS install is production ({verdict.words}); disk pressure for a "
                      "test is only for a development install")
    summary = storage_watch.summary(connection, host_id)
    if summary.get("stale"):
        raise Refused("the storage reading is stale (the host helper is not answering: boss "
                      "steward status); nothing was changed")
    free = (summary.get("disk") or {}).get("free_pct")
    if free is None:
        raise Refused("BOSS has no storage reading yet; nothing was changed")
    host = summary.get("host_id") or host_id
    row = _row(connection, host)
    rule = _rule(row)
    mark = rule.get(MARK) if isinstance(rule.get(MARK), dict) else None
    current = storage_rules.thresholds(connection, host)
    # A second move keeps the FIRST move's originals: off puts back what the person had.
    before = dict((mark or {}).get("moved") or {})
    moved = dict(current)
    for key, margin in MOVES[level].items():
        target = round(min(95.0, float(free) + margin), 1)
        moved[key] = max(float(current[key]), target)     # never lowered
    moved["warn_exit_free_pct"] = max(float(current["warn_exit_free_pct"]),
                                      moved["warn_free_pct"] + 4.0)
    moved["target_free_pct"] = max(float(current["target_free_pct"]),
                                   moved["critical_free_pct"] + 6.0)
    storage_rules.check_order(moved)
    changed = {k: [before.get(k, [current[k]])[0], moved[k]] for k in moved
               if moved[k] != current[k] or k in before}
    source = (mark or {}).get("source_before") or str((row or {}).get("source") or "default")
    out = storage_rules.upsert(connection, kind="threshold", rule=moved,
                               # (the rule's source column takes only BOSS's own words; the mark
                               # is what says "a test", and restore puts the source back)
                               host_id=(row or {}).get("host_id"), source="operator", actor=actor)
    until = (_now() + timedelta(hours=hours)).isoformat()
    new_mark = {"level": level, "until": until, "by": actor, "source_before": source,
                "free_pct": round(float(free), 1), "moved": changed}
    connection.execute(text(
        "UPDATE retention_rules SET rule = rule || jsonb_build_object(CAST(:k AS TEXT), CAST(:m AS JSONB)) "
        "WHERE id = :i"), {"k": MARK, "m": json.dumps(new_mark), "i": out["id"]})
    return {**new_mark, "thresholds": moved}


def restore(connection: Any, *, actor: str, host_id: Optional[str] = None,
            why: str = "the test ended") -> dict:
    """Put back what the test moved, key by key, only where the value is still the test's."""
    row = _row(connection, host_id)
    rule = _rule(row)
    mark = rule.get(MARK)
    if row is None or not isinstance(mark, dict):
        return {"restored": {}, "kept": {}, "why": "nothing was moved for a test"}
    current = storage_rules.thresholds(connection, row.get("host_id"))
    restored, kept = {}, {}
    back = dict(current)
    for key, (before, set_to) in (mark.get("moved") or {}).items():
        if key not in current:
            continue
        if float(current[key]) == float(set_to):
            back[key] = float(before)
            restored[key] = float(before)
        else:
            kept[key] = float(current[key])               # a person changed it since
    clean = {k: v for k, v in rule.items() if k != MARK}
    clean.update(back)
    connection.execute(text(
        "UPDATE retention_rules SET rule = CAST(:r AS JSONB), source = :s, updated_at = now() "
        "WHERE id = :i"), {"r": json.dumps(clean), "s": str(mark.get("source_before") or "default"),
                           "i": row["id"]})
    try:
        from .core import emit_event

        pid = connection.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
        if pid:
            emit_event(connection, pid, "storage.qa_pressure_restored", actor, "retention_rule",
                       row["id"], {"restored": restored, "kept": kept, "why": why})
    except Exception:  # noqa: BLE001 — the record is bookkeeping; the restore stands
        pass
    return {"restored": restored, "kept": kept, "why": why}


def restore_all(connection: Any, *, actor: str, why: str = "the test ended") -> dict:
    """`off`: every threshold rule a test marked (any host), each put back as `restore` does."""
    restored: dict = {}
    kept: dict = {}
    for (host,) in connection.execute(text(
            "SELECT host_id FROM retention_rules WHERE kind = 'threshold' "
            "AND rule -> CAST(:k AS TEXT) IS NOT NULL"), {"k": MARK}).all():
        out = restore(connection, actor=actor, host_id=host, why=why)
        restored.update(out["restored"])
        kept.update(out["kept"])
    return {"restored": restored, "kept": kept, "why": why}


def expire(connection: Any) -> Optional[dict]:
    """The storage watch's tick: a test move past its `until` is restored by itself."""
    for row in connection.execute(text(
            "SELECT host_id, rule FROM retention_rules WHERE kind = 'threshold' "
            "AND rule -> CAST(:k AS TEXT) IS NOT NULL"), {"k": MARK}).mappings().all():
        mark = _rule(row).get(MARK) or {}
        try:
            until = datetime.fromisoformat(str(mark.get("until")))
        except ValueError:
            until = _now()
        if until <= _now():
            return restore(connection, actor="boss", host_id=row["host_id"],
                           why="the test's time ran out")
    return None
