"""THE STORAGE WATCH: readings → forecast → level with hysteresis → incidents (LLD §4.1).

Every 30 s per host a reading lands in `storage_samples` (the host helper's `df` / `docker system
df` / backup-dir walk; the API's own Postgres figures; `boss-hostd` on KVM hosts). From the last
seven days the watch fits a slope — `growth_gb_per_day` — and says "full in N days". Two readings
in a row must agree before a level changes (one blip never opens an incident), and the exit band
sits above the entry band, so a host hovering at the line does not flap.

    warn      free < 20 % or full in < 7 d        exit: free ≥ 24 % and full in ≥ 10 d
    critical  free < 15 % or full in < 3 d or pg temp > 5 GB or Docker.raw unreclaimed > 40 GB
              with free < 20 %                    exit: free ≥ 25 % (target) and pg temp < 1 GB
    floor     free < 10 % or < 5 GB               admission refuses heavy jobs (Layer 1)

The numbers are settings (`retention_rules` kind `threshold`, storage_rules.DEFAULT_THRESHOLDS).
The watch is a system service (`system_services` key `storage_watch`), not an agent: it opens the
incident; the Infra Agent explains and asks; the executor cleans. It runs in the API like the
doctor, beside the builds it watches.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from . import storage_rules

_log = logging.getLogger("boss.storage_watch")

INTERVAL_SECONDS = 30.0
KEEP_DAYS = 30
FORECAST_WINDOW_DAYS = 7
#: A reading older than this is stale; the Storage card says so.
STALE_AFTER = timedelta(minutes=2)
#: No helper reading for this long during an incident = "helper unreachable" (spec `escalate`).
HELPER_UNREACHABLE_AFTER = timedelta(minutes=10)
#: A hold whose host stopped reporting for this long auto-releases if the last reading was fine.
STUCK_HOLD_AFTER = timedelta(minutes=15)
LEVELS = ("ok", "warn", "critical")
SOURCES = ("steward", "hostd", "api")
#: The by_type keys the Storage card draws, in order; `free` is appended by the summary.
TYPE_KEYS = ("backups", "images", "build_cache", "volumes", "logs", "pg_db", "pg_temp",
             "docker_vm_unreclaimed", "other")
TYPE_LABELS = {"backups": "Backups", "images": "Images", "build_cache": "Build cache",
               "volumes": "Volumes", "logs": "Logs", "pg_db": "Database", "pg_temp": "DB temp",
               "docker_vm_unreclaimed": "Docker VM unreclaimed", "other": "Other", "free": "Free"}
REBUILDABLE = frozenset({"images", "build_cache", "logs", "pg_temp", "docker_vm_unreclaimed"})

_SAMPLE_FIELDS = ("total_gb", "free_gb", "used_pct", "docker_alloc_gb", "docker_used_gb",
                  "images_gb", "cache_gb", "volumes_gb", "logs_gb", "backups_gb", "backups_n",
                  "pg_temp_gb", "pg_db_gb")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def local_host_id() -> str:
    """The id the API and the helper on the same machine agree on: BOSS_HOST_ID, else the
    install id, else the hostname."""
    for name in ("BOSS_HOST_ID", "BOSS_INSTALL_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:120]
    return socket.gethostname()[:120]


def local_host_label() -> str:
    return (os.environ.get("BOSS_HOST_LABEL") or "").strip()[:80] or "this host"


# ── forecast (pure) ──────────────────────────────────────────────────────────────────────────

@dataclass
class Forecast:
    growth_gb_per_day: float = 0.0
    days_until_full: Optional[float] = None      # None = not growing
    full_at: Optional[str] = None
    window_days: int = FORECAST_WINDOW_DAYS
    samples: int = 0
    with_rules_days_until_full: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {"growth_gb_per_day": round(self.growth_gb_per_day, 3),
                "days_until_full": None if self.days_until_full is None
                else round(self.days_until_full, 1),
                "full_at": self.full_at, "window_days": self.window_days, "samples": self.samples,
                "with_rules_days_until_full": None if self.with_rules_days_until_full is None
                else round(self.with_rules_days_until_full, 1)}


def slope_gb_per_day(points: Sequence[tuple[datetime, float]]) -> float:
    """Least-squares slope of used GB over time, in GB per day. 0 with fewer than two points."""
    if len(points) < 2:
        return 0.0
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in points]
    ys = [float(v) for _, v in points]
    n = float(len(points))
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / var


def forecast(points: Sequence[tuple[datetime, float]], free_gb: Optional[float], *,
             now: Optional[datetime] = None, rules_reclaim_gb: float = 0.0,
             window_days: int = FORECAST_WINDOW_DAYS) -> Forecast:
    """`days_until_full = free_gb / max(slope, 0.1)` when the disk is growing; None (never)
    when the slope is zero or negative. `rules_reclaim_gb` is what the enabled rules would
    hand back, for the dashed green "with rules" line."""
    now = now or _now()
    since = now - timedelta(days=window_days)
    window = [(t, v) for t, v in points if t >= since]
    slope = slope_gb_per_day(window)
    out = Forecast(growth_gb_per_day=slope, samples=len(window), window_days=window_days)
    if free_gb is None or slope <= 0:
        return out
    rate = max(slope, 0.1)
    out.days_until_full = float(free_gb) / rate
    out.full_at = (now + timedelta(days=out.days_until_full)).isoformat()
    if rules_reclaim_gb > 0:
        out.with_rules_days_until_full = (float(free_gb) + rules_reclaim_gb) / rate
    return out


# ── levels with hysteresis (pure) ────────────────────────────────────────────────────────────

def free_pct_of(sample: Mapping[str, Any]) -> Optional[float]:
    total, free = _f(sample.get("total_gb")), _f(sample.get("free_gb"))
    if total and free is not None and total > 0:
        return 100.0 * free / total
    used = _f(sample.get("used_pct"))
    return None if used is None else 100.0 - used


def raw_level(sample: Mapping[str, Any], days_until_full: Optional[float],
              th: Mapping[str, float]) -> str:
    """The level ONE reading says, no memory: ok | warn | critical."""
    free_pct = free_pct_of(sample)
    if free_pct is None:
        return "ok"
    pg_temp = _f(sample.get("pg_temp_gb")) or 0.0
    alloc, used = _f(sample.get("docker_alloc_gb")), _f(sample.get("docker_used_gb"))
    unreclaimed = (alloc - used) if (alloc is not None and used is not None) else 0.0
    days = days_until_full
    if (free_pct < th["critical_free_pct"]
            or (days is not None and days < th["critical_days"])
            or pg_temp > th["pg_temp_critical_gb"]
            or (unreclaimed > th["docker_unreclaimed_gb"] and free_pct < th["warn_free_pct"])):
        return "critical"
    if free_pct < th["warn_free_pct"] or (days is not None and days < th["warn_days"]):
        return "warn"
    return "ok"


def at_floor(sample: Mapping[str, Any], th: Mapping[str, float]) -> bool:
    free_pct, free_gb = free_pct_of(sample), _f(sample.get("free_gb"))
    return bool((free_pct is not None and free_pct < th["floor_free_pct"])
                or (free_gb is not None and free_gb < th["floor_free_gb"]))


def exit_ok(level: str, sample: Mapping[str, Any], days_until_full: Optional[float],
            th: Mapping[str, float]) -> bool:
    """Does ONE reading sit in the exit band of `level`?"""
    free_pct = free_pct_of(sample)
    if free_pct is None:
        return False
    days = days_until_full
    pg_temp = _f(sample.get("pg_temp_gb")) or 0.0
    if level == "critical":
        return free_pct >= th["target_free_pct"] and pg_temp < th["pg_temp_exit_gb"]
    if level == "warn":
        return free_pct >= th["warn_exit_free_pct"] and (days is None or days >= th["warn_exit_days"])
    return True


def next_level(previous: str, readings: Sequence[tuple[Mapping[str, Any], Optional[float]]],
               th: Mapping[str, float]) -> str:
    """HYSTERESIS: `readings` are the newest readings, oldest first, each (sample, days_until_full).
    A level is entered or escalated only when the last TWO readings both say at least that level;
    a level is left only when the last two both sit in its exit band. Otherwise it stays."""
    previous = previous if previous in LEVELS else "ok"
    if len(readings) < 2:
        return previous
    last_two = list(readings)[-2:]
    raws = [raw_level(s, d, th) for s, d in last_two]
    agreed = min(raws, key=LEVELS.index)            # the level both readings reach
    if LEVELS.index(agreed) > LEVELS.index(previous):
        return agreed                                # enter / escalate
    if previous != "ok" and all(exit_ok(previous, s, d, th) for s, d in last_two):
        # Both in the exit band: down to whatever both still reach (usually ok).
        return agreed if LEVELS.index(agreed) < LEVELS.index(previous) else previous
    return previous


# ── readings ────────────────────────────────────────────────────────────────────────────────

def clean_sample(payload: Mapping[str, Any], *, source: str,
                 default_host: Optional[str] = None) -> dict[str, Any]:
    """A reading as the table wants it; unknown numbers are left NULL, never guessed."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    host_id = str(payload.get("host_id") or default_host or local_host_id())[:120]
    out: dict[str, Any] = {"host_id": host_id, "source": source,
                           "host_label": str(payload.get("host_label") or "")[:80] or None,
                           "at": _now()}
    raw_at = payload.get("at")
    if isinstance(raw_at, str):
        try:
            parsed = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # A clock far off is replaced by ours; a few minutes either way is kept.
            if abs((parsed - out["at"]).total_seconds()) < 600:
                out["at"] = parsed
        except ValueError:
            pass
    for name in _SAMPLE_FIELDS:
        value = _f(payload.get(name))
        out[name] = None if value is None else (int(value) if name == "backups_n" else round(value, 3))
    if out["used_pct"] is None and out["total_gb"] and out["free_gb"] is not None and out["total_gb"] > 0:
        out["used_pct"] = round(100.0 * (out["total_gb"] - out["free_gb"]) / out["total_gb"], 2)
    by_type = payload.get("by_type") or {}
    out["by_type"] = {str(k)[:40]: round(float(v), 3) for k, v in dict(by_type).items()
                      if _f(v) is not None} if isinstance(by_type, Mapping) else {}
    return out


def record_sample(connection: Any, sample: Mapping[str, Any]) -> dict[str, Any]:
    """Insert one reading (upsert on (host, at): two helpers never write two rows for one second)."""
    row = dict(sample)
    connection.execute(text("""
        INSERT INTO storage_samples (host_id, at, source, total_gb, free_gb, used_pct,
               docker_alloc_gb, docker_used_gb, images_gb, cache_gb, volumes_gb, logs_gb,
               backups_gb, backups_n, pg_temp_gb, pg_db_gb, by_type, host_label)
        VALUES (:host_id, :at, :source, :total_gb, :free_gb, :used_pct, :docker_alloc_gb,
                :docker_used_gb, :images_gb, :cache_gb, :volumes_gb, :logs_gb, :backups_gb,
                :backups_n, :pg_temp_gb, :pg_db_gb, CAST(:by_type AS JSONB), :host_label)
        ON CONFLICT (host_id, at) DO UPDATE SET source = EXCLUDED.source,
               total_gb = COALESCE(EXCLUDED.total_gb, storage_samples.total_gb),
               free_gb = COALESCE(EXCLUDED.free_gb, storage_samples.free_gb),
               used_pct = COALESCE(EXCLUDED.used_pct, storage_samples.used_pct),
               by_type = storage_samples.by_type || EXCLUDED.by_type"""),
        {**{k: row.get(k) for k in ("host_id", "at", "source", "host_label", *_SAMPLE_FIELDS)},
         "by_type": json.dumps(row.get("by_type") or {})})
    return row


def collect_api_sample(connection: Any) -> dict[str, Any]:
    """What the API can measure itself: Postgres temp files and database sizes, the backups
    table, the container's own `/` as a fallback reading of the disk."""
    out: dict[str, Any] = {"host_id": local_host_id(), "host_label": local_host_label(),
                           "at": _now().isoformat()}
    try:
        usage = shutil.disk_usage("/")
        out["total_gb"] = usage.total / 1e9
        out["free_gb"] = usage.free / 1e9
    except OSError:
        pass
    try:
        out["pg_db_gb"] = float(connection.execute(text(
            "SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database "
            "WHERE NOT datistemplate")).scalar() or 0) / 1e9
    except Exception:  # noqa: BLE001 — a role without the right is a NULL reading
        pass
    try:
        out["pg_temp_gb"] = float(connection.execute(text(
            "SELECT COALESCE(sum(size), 0) FROM pg_ls_tmpdir()")).scalar() or 0) / 1e9
    except Exception:  # noqa: BLE001
        pass
    try:
        row = connection.execute(text(
            "SELECT count(*) AS n, COALESCE(sum(bytes), 0) AS b FROM backups "
            "WHERE status = 'verified'")).mappings().one()
        out["backups_n"] = int(row["n"] or 0)
        out["backups_gb"] = float(row["b"] or 0) / 1e9
    except Exception:  # noqa: BLE001
        pass
    return out


def latest(connection: Any, host_id: str, *, within: timedelta = timedelta(minutes=10)
           ) -> Optional[dict[str, Any]]:
    """The newest reading of a host, with NULL figures filled from the newest reading of each
    other source inside `within` (the helper knows the disk; the API knows Postgres)."""
    rows = connection.execute(text(
        "SELECT * FROM storage_samples WHERE host_id = :h AND at >= :since "
        "ORDER BY at DESC LIMIT 40"), {"h": host_id, "since": _now() - within}).mappings().all()
    if not rows:
        return None
    merged: dict[str, Any] = dict(rows[0])
    seen = {rows[0]["source"]}
    for r in rows[1:]:
        if r["source"] in seen:
            continue
        seen.add(r["source"])
        for k in _SAMPLE_FIELDS:
            if merged.get(k) is None and r.get(k) is not None:
                merged[k] = r[k]
        merged["by_type"] = {**(r.get("by_type") or {}), **(merged.get("by_type") or {})}
        if not merged.get("host_label") and r.get("host_label"):
            merged["host_label"] = r["host_label"]
    for k in _SAMPLE_FIELDS:
        if merged.get(k) is not None:
            merged[k] = float(merged[k]) if k != "backups_n" else int(merged[k])
    merged["sources"] = sorted(seen)
    return merged


def last_two(connection: Any, host_id: str) -> list[dict[str, Any]]:
    """The two newest DISK readings (rows that carry free_gb), oldest first."""
    rows = connection.execute(text(
        "SELECT * FROM storage_samples WHERE host_id = :h AND free_gb IS NOT NULL "
        "ORDER BY at DESC LIMIT 2"), {"h": host_id}).mappings().all()
    return [dict(r) for r in reversed(rows)]


def used_series(connection: Any, host_id: str, *, days: int = KEEP_DAYS,
                max_points: int = 240) -> list[tuple[datetime, float, float]]:
    """(at, used_gb, free_gb), oldest first, downsampled to ≤ max_points."""
    rows = connection.execute(text(
        "SELECT at, total_gb, free_gb FROM storage_samples WHERE host_id = :h "
        "AND free_gb IS NOT NULL AND total_gb IS NOT NULL AND at >= :since ORDER BY at"),
        {"h": host_id, "since": _now() - timedelta(days=days)}).all()
    pts = [(r[0] if r[0].tzinfo else r[0].replace(tzinfo=timezone.utc),
            float(r[1]) - float(r[2]), float(r[2])) for r in rows]
    if len(pts) > max_points:
        step = len(pts) / float(max_points)
        pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
    return pts


def hosts(connection: Any, *, within: timedelta = timedelta(days=2)) -> list[str]:
    return [str(r[0]) for r in connection.execute(text(
        "SELECT host_id FROM storage_samples WHERE at >= :since GROUP BY host_id "
        "ORDER BY max(at) DESC"), {"since": _now() - within}).all()]


def prune(connection: Any) -> int:
    return int(connection.execute(text(
        "WITH gone AS (DELETE FROM storage_samples WHERE at < :cut RETURNING 1) "
        "SELECT count(*) FROM gone"), {"cut": _now() - timedelta(days=KEEP_DAYS)}).scalar() or 0)


# ── the reading the console and the agent see ───────────────────────────────────────────────

def by_type_rows(sample: Mapping[str, Any]) -> list[dict[str, Any]]:
    total = _f(sample.get("total_gb")) or 0.0
    free = _f(sample.get("free_gb")) or 0.0
    alloc, used = _f(sample.get("docker_alloc_gb")), _f(sample.get("docker_used_gb"))
    unreclaimed = max(0.0, (alloc or 0.0) - (used or 0.0)) if alloc is not None and used is not None else 0.0
    sizes = {"backups": _f(sample.get("backups_gb")) or 0.0,
             "images": _f(sample.get("images_gb")) or 0.0,
             "build_cache": _f(sample.get("cache_gb")) or 0.0,
             "volumes": _f(sample.get("volumes_gb")) or 0.0,
             "logs": _f(sample.get("logs_gb")) or 0.0,
             "pg_db": _f(sample.get("pg_db_gb")) or 0.0,
             "pg_temp": _f(sample.get("pg_temp_gb")) or 0.0,
             "docker_vm_unreclaimed": unreclaimed}
    for k, v in (sample.get("by_type") or {}).items():
        if k in TYPE_LABELS and k not in ("other", "free") and _f(v) is not None and not sizes.get(k):
            sizes[k] = float(v)
    known = sum(sizes.values())
    sizes["other"] = max(0.0, total - free - known)
    rows = [{"key": k, "label": TYPE_LABELS[k], "gb": round(sizes[k], 2),
             "pct": round(100.0 * sizes[k] / total, 2) if total else 0.0,
             "rebuildable": k in REBUILDABLE} for k in TYPE_KEYS]
    rows.append({"key": "free", "label": "Free", "gb": round(free, 2),
                 "pct": round(100.0 * free / total, 2) if total else 0.0, "rebuildable": False})
    return rows


def rules_reclaim_gb(sample: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> float:
    """What the ENABLED rules would hand back now (for the "with rules" forecast line)."""
    enabled = {r["kind"]: r["rule"] for r in rules if r.get("enabled")}
    gain = 0.0
    if "build_cache" in enabled:
        gain += max(0.0, (_f(sample.get("cache_gb")) or 0.0) - float(enabled["build_cache"].get("cap_gb", 10)))
    if "backups" in enabled:
        cap = float(enabled["backups"].get("cap_gb", 20))
        gain += max(0.0, (_f(sample.get("backups_gb")) or 0.0) - cap)
    if "docker_trim" in enabled and enabled["docker_trim"].get("enabled", True):
        alloc, used = _f(sample.get("docker_alloc_gb")), _f(sample.get("docker_used_gb"))
        if alloc is not None and used is not None:
            gain += max(0.0, alloc - used)
    if "images" in enabled:
        gain += 0.5 * (_f(sample.get("images_gb")) or 0.0)   # old versions are roughly half
    gain += _f(sample.get("pg_temp_gb")) or 0.0
    return round(gain, 2)


def summary(connection: Any, host_id: Optional[str] = None) -> dict[str, Any]:
    """`GET /v1/infra/storage/summary` (docs/infra/INFRA_AGENT_API.md §4)."""
    from . import storage_incidents

    host_id = host_id or (hosts(connection) or [local_host_id()])[0]
    th = storage_rules.thresholds(connection, host_id)
    rules = storage_rules.list_rules(connection, host_id)
    sample = latest(connection, host_id, within=timedelta(days=2)) or {}
    series = used_series(connection, host_id)
    fc = forecast([(t, u) for t, u, _ in series], _f(sample.get("free_gb")),
                  rules_reclaim_gb=rules_reclaim_gb(sample, rules))
    at = sample.get("at")
    stale = at is None or (_now() - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))) > STALE_AFTER
    free_pct = free_pct_of(sample)
    level = raw_level(sample, fc.days_until_full, th) if sample else "ok"
    if sample and at_floor(sample, th):
        level = "floor"
    open_incident = storage_incidents.current_for_host(connection, host_id)
    last = storage_incidents.last_resolved(connection, host_id)
    try:
        from . import telemetry_retention

        with connection.begin_nested():
            telemetry = telemetry_retention.share(connection)
    except Exception:  # noqa: BLE001 — the card renders without it
        telemetry = None
    host_rows = []
    for h in hosts(connection):
        s = latest(connection, h, within=timedelta(days=2)) or {}
        ser = used_series(connection, h, days=7, max_points=24)
        f = forecast([(t, u) for t, u, _ in ser], _f(s.get("free_gb")))
        fp = free_pct_of(s)
        host_rows.append({"host_id": h, "host_label": s.get("host_label") or h,
                          "free_pct": round(fp, 1) if fp is not None else None,
                          "days_until_full": None if f.days_until_full is None else round(f.days_until_full, 1),
                          "level": raw_level(s, f.days_until_full, th) if s else "ok",
                          "spark": [round(fr, 1) for _, _, fr in ser]})
    return {"host_id": host_id, "host_label": sample.get("host_label") or local_host_label(),
            "sampled_at": _iso(at), "source": sample.get("source"), "stale": stale,
            "disk": {"total_gb": round(_f(sample.get("total_gb")) or 0.0, 2),
                     "free_gb": round(_f(sample.get("free_gb")) or 0.0, 2),
                     "used_pct": round(100.0 - free_pct, 2) if free_pct is not None else None,
                     "free_pct": round(free_pct, 2) if free_pct is not None else None},
            "by_type": by_type_rows(sample), "forecast": fc.as_dict(),
            "trend": [{"at": t.isoformat(), "used_gb": round(u, 2), "free_gb": round(fr, 2)}
                      for t, u, fr in series],
            "thresholds": th, "level": level, "rules": rules,
            "last_cleanup": last, "open_incident_id": (open_incident or {}).get("id"),
            # ST-8: telemetry tables' share of the database and the last retention sweep.
            "telemetry": telemetry,
            "hosts": host_rows}


# ── the tick ────────────────────────────────────────────────────────────────────────────────

_LAST: dict[str, Any] = {}
_THREAD: Optional[threading.Thread] = None


def evaluate_host(connection: Any, host_id: str) -> dict[str, Any]:
    """One host: the last two disk readings → level → the incident machine."""
    from . import storage_incidents

    th = storage_rules.thresholds(connection, host_id)
    pair = last_two(connection, host_id)
    if not pair:
        return {"host_id": host_id, "level": "ok", "readings": 0}
    merged = latest(connection, host_id, within=timedelta(days=2)) or pair[-1]
    series = used_series(connection, host_id, days=FORECAST_WINDOW_DAYS, max_points=400)
    fc = forecast([(t, u) for t, u, _ in series], _f(merged.get("free_gb")))
    readings = []
    for s in pair:
        s2 = {**merged, **{k: v for k, v in s.items() if v is not None}}
        readings.append((s2, fc.days_until_full))
    incident = storage_incidents.current_for_host(connection, host_id)
    previous = storage_incidents.level_of(incident)
    level = next_level(previous, readings, th)
    floor = at_floor(readings[-1][0], th)
    outcome = storage_incidents.on_level(connection, host_id=host_id, level=level,
                                         previous=previous, sample=readings[-1][0], forecast=fc,
                                         thresholds=th, floor=floor)
    return {"host_id": host_id, "level": level, "previous": previous, "floor": floor,
            "readings": len(pair), **outcome}


def tick(engine: Any, settings: Any = None, *, sample_api: bool = True) -> dict[str, Any]:
    from . import storage_cleanup, storage_incidents, system_services, work_hold

    report: dict[str, Any] = {"at": _now().isoformat(), "hosts": []}
    try:
        with engine.begin() as c:
            storage_rules.ensure_defaults(c)
            if sample_api:
                record_sample(c, clean_sample(collect_api_sample(c), source="api"))
            for host_id in hosts(c, within=timedelta(minutes=10)):
                try:
                    with c.begin_nested():
                        report["hosts"].append(evaluate_host(c, host_id))
                except Exception as exc:  # noqa: BLE001 — one host never stops the others
                    _log.warning("storage watch: host %s not evaluated", host_id, exc_info=True)
                    report["hosts"].append({"host_id": host_id, "error": f"{type(exc).__name__}"})
            # A stuck hold: incident gone, or the host silent for 15 min with a good last reading,
            # or the hold older than the maximum (BOSS_STORAGE_MAX_HOLD_SECONDS, 2 h): released,
            # the incident kept open, an alert raised.
            hold = work_hold.state(c, fresh=True)
            if hold.get("active"):
                inc = storage_incidents.get(c, str(hold.get("incident_id") or ""))
                max_seconds = int(getattr(settings, "storage_max_hold_seconds", 7200) or 7200)
                if inc is None or inc["status"] == "resolved":
                    work_hold.release(c, actor="boss", reason="its incident is resolved")
                elif work_hold.expired(hold, max_seconds):
                    storage_incidents.expire_hold(c, inc, max_seconds=max_seconds)
                else:
                    s = latest(c, str(hold.get("host_id") or ""), within=timedelta(days=2)) or {}
                    at = s.get("at")
                    th = storage_rules.thresholds(c, str(hold.get("host_id") or ""))
                    fp = free_pct_of(s)
                    if at is not None and (_now() - (at if at.tzinfo else at.replace(
                            tzinfo=timezone.utc))) > STUCK_HOLD_AFTER and fp is not None \
                            and fp >= th["target_free_pct"]:
                        storage_incidents.resolve(c, inc["id"], reason="no reading for 15 min; "
                                                  "the last one was above target")
            if int(time.time()) % 600 < INTERVAL_SECONDS:
                report["pruned"] = prune(c)
        # Plans advance outside the sampling transaction: host jobs, receipts, cards.
        report["advanced"] = storage_cleanup.advance_all(engine)
        system_services.beat(engine, "storage_watch",
                             details={"hosts": len(report["hosts"]),
                                      "levels": {h["host_id"]: h.get("level") for h in report["hosts"]}})
    except Exception as exc:  # noqa: BLE001 — the watch keeps watching
        _log.warning("storage watch tick failed", exc_info=True)
        report["error"] = f"{type(exc).__name__}: {exc}"[:200]
        try:
            system_services.beat(engine, "storage_watch", ok=False, error=report["error"])
        except Exception:  # noqa: BLE001
            pass
    _LAST.clear()
    _LAST.update(report)
    return report


def last_report() -> dict[str, Any]:
    return dict(_LAST)


def start(engine: Any, settings: Any, *, interval: float = INTERVAL_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                tick(engine, settings)
            except Exception:  # noqa: BLE001
                _log.debug("storage watch tick failed", exc_info=True)

    _THREAD = threading.Thread(target=_loop, name="boss-storage-watch", daemon=True)
    _THREAD.start()
    return _THREAD


__all__ = ["FORECAST_WINDOW_DAYS", "Forecast", "HELPER_UNREACHABLE_AFTER", "INTERVAL_SECONDS",
           "KEEP_DAYS", "LEVELS", "STALE_AFTER", "at_floor", "by_type_rows", "clean_sample",
           "collect_api_sample", "evaluate_host", "exit_ok", "forecast", "free_pct_of", "hosts",
           "last_report", "last_two", "latest", "local_host_id", "next_level", "prune", "raw_level",
           "record_sample", "slope_gb_per_day", "start", "summary", "tick", "used_series"]
