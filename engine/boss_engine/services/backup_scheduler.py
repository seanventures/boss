"""SCHEDULED SELF-BACKUPS (BOSSAPP-838): BOSS backs its own database up, on its own.

`scripts/install/backup.sh` makes a full recovery archive, but only when a person runs it (or an
upgrade does). Between upgrades nothing was kept, so a bad migration, a dropped table or a disk
that went read-only lost everything since the last upgrade. This thread closes that gap:

* EXACTLY ONE RUNS. Every API process starts the thread; each tick it takes (or renews) the
  `backup_scheduler_lease` row. Only the holder backs up; a holder that dies stops renewing and
  another process takes the lease once it expires.
* EVERY `BOSS_BACKUP_INTERVAL_MINUTES` (default hourly) it runs `pg_dump -Fc` into
  `BOSS_BACKUP_DIR` (the `boss_community_backups` volume on an install) — the password goes in the
  child's environment, never its argv — and records one `backups` row per attempt.
* EVERY DUMP IS VERIFIED with `pg_restore --list` before it counts. A dump that does not list is
  `failed`, its file removed, and the system doctor says so (backup_failed).
* OPTIONAL OFF-HOST COPY: with BOSS_BACKUP_S3_UPLOAD=true and the S3_* settings (S3_ENDPOINT for
  MinIO), each verified dump is uploaded; an upload that fails keeps the local copy and says so.
* TIERED RETENTION (grandfather-father-son): the newest `keep_hourly` verified dumps, plus the
  newest of each of the last `keep_daily` days, `keep_weekly` ISO weeks and `keep_monthly`
  months. Everything else is removed (file and uploaded object) and its row marked `pruned`.
* HEALTH: `health()` reads the rows — ok / stale / failed / missing / pending / disabled — and the
  system doctor turns stale, failed and missing into a card (system_doctor.diagnose).

This is the database only. backup.sh remains the full recovery archive (.env, artifacts, identity
and repository volumes) that upgrades take; `boss rollback --restore-backup` restores one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.backup_scheduler")

LEASE_NAME = "backup_scheduler"
LEASE_TTL = timedelta(minutes=10)
TICK_SECONDS = 60.0
FIRST_TICK_DELAY = 90.0
#: A `running` row older than this belongs to a process that died mid-dump.
INTERRUPTED_AFTER = timedelta(hours=3)
DUMP_TIMEOUT_SECONDS = 2 * 60 * 60
VERIFY_TIMEOUT_SECONDS = 10 * 60

HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Policy:
    keep_hourly: int = 24
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6

    @classmethod
    def from_settings(cls, settings: Any) -> "Policy":
        return cls(keep_hourly=int(getattr(settings, "backup_keep_hourly", 24)),
                   keep_daily=int(getattr(settings, "backup_keep_daily", 7)),
                   keep_weekly=int(getattr(settings, "backup_keep_weekly", 4)),
                   keep_monthly=int(getattr(settings, "backup_keep_monthly", 6)))


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _settings(settings: Any) -> Any:
    if settings is not None:
        return settings
    from ..config import get_settings

    return get_settings()


def _interval(settings: Any) -> timedelta:
    return timedelta(minutes=int(getattr(settings, "backup_interval_minutes", 60)))


# ------------------------------------------------------------------------------------------ #
# retention — pure, so it can be tested with any clock
# ------------------------------------------------------------------------------------------ #

def retention_keep(rows: Iterable[Mapping[str, Any]], policy: Policy) -> set[str]:
    """The ids of verified backups the tiers keep. `rows`: dicts with id and started_at.

    Each tier keeps the NEWEST backup in each of its most recent N buckets (hourly is simply the
    newest N). A backup kept by any tier is kept; the newest one is always kept."""
    ordered = sorted(rows, key=lambda r: _utc(r["started_at"]), reverse=True)
    keep: set[str] = {str(r["id"]) for r in ordered[: max(1, policy.keep_hourly)]}

    def tier(bucket: Callable[[datetime], Any], count: int) -> None:
        seen: list[Any] = []
        for row in ordered:
            key = bucket(_utc(row["started_at"]))
            if key in seen:
                continue
            if len(seen) >= count:
                break
            seen.append(key)
            keep.add(str(row["id"]))

    tier(lambda t: t.date(), policy.keep_daily)
    tier(lambda t: tuple(t.isocalendar())[:2], policy.keep_weekly)
    tier(lambda t: (t.year, t.month), policy.keep_monthly)
    return keep


# ------------------------------------------------------------------------------------------ #
# the lease
# ------------------------------------------------------------------------------------------ #

def acquire_lease(connection: Any, holder: str, now: datetime,
                  ttl: timedelta = LEASE_TTL) -> bool:
    """Take the lease if it is free, expired or already ours; renew it. True when we hold it."""
    row = connection.execute(text(
        "INSERT INTO backup_scheduler_lease (name, holder, expires_at, first_seen_at) "
        "VALUES (:n, :h, :exp, :now) "
        "ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, "
        "expires_at = EXCLUDED.expires_at "
        "WHERE backup_scheduler_lease.holder = EXCLUDED.holder "
        "OR backup_scheduler_lease.expires_at < :now "
        "RETURNING holder"), {"n": LEASE_NAME, "h": holder, "exp": now + ttl, "now": now}).first()
    return bool(row and row[0] == holder)


def release_lease(connection: Any, holder: str) -> None:
    connection.execute(text("UPDATE backup_scheduler_lease SET expires_at = now() - interval "
                            "'1 second' WHERE name = :n AND holder = :h"),
                       {"n": LEASE_NAME, "h": holder})


# ------------------------------------------------------------------------------------------ #
# one backup
# ------------------------------------------------------------------------------------------ #

def mark_interrupted(connection: Any, now: datetime) -> int:
    return connection.execute(text(
        "UPDATE backups SET status = 'failed', finished_at = :now, "
        "error = 'interrupted: the process taking it stopped' "
        "WHERE status = 'running' AND started_at < :cut"),
        {"now": now, "cut": now - INTERRUPTED_AFTER}).rowcount or 0


def is_due(connection: Any, settings: Any, now: datetime) -> bool:
    last = connection.execute(text(
        "SELECT max(started_at) FROM backups WHERE kind = 'scheduled'")).scalar()
    return last is None or now - _utc(last) >= _interval(settings)


def _pg_env(database_url: str) -> dict[str, str]:
    """libpq variables for the dump: the password reaches the child's environment, not argv."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update({"PGHOST": url.host or "localhost", "PGPORT": str(url.port or 5432),
                "PGUSER": url.username or "", "PGDATABASE": url.database or ""})
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _short(text_: Any, limit: int = 400) -> str:
    return " ".join(str(text_ or "").split())[:limit]


def verify(path: Path, settings: Any, runner: Runner = subprocess.run) -> Optional[str]:
    """None when `pg_restore --list` reads the archive; otherwise why it could not."""
    try:
        done = runner([getattr(settings, "backup_pg_restore", "pg_restore"), "--list", str(path)],
                      capture_output=True, text=True, timeout=VERIFY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"verification could not run: {type(exc).__name__}: {_short(exc)}"
    if done.returncode != 0:
        return f"pg_restore --list failed ({done.returncode}): {_short(done.stderr)}"
    if not str(done.stdout or "").strip():
        return "pg_restore --list read an empty archive"
    return None


def s3_uploader(settings: Any) -> Optional[Callable[[Path, str], None]]:
    """An uploader when off-host copies are on and configured; None otherwise."""
    if not getattr(settings, "backup_s3_upload", False) or not getattr(settings, "s3_bucket", None):
        return None

    def upload(path: Path, key: str) -> None:
        import boto3

        client = boto3.client("s3", endpoint_url=settings.s3_endpoint or None,
                              aws_access_key_id=settings.s3_access_key_id or None,
                              aws_secret_access_key=settings.s3_secret_access_key or None,
                              region_name=settings.s3_region or None)
        client.upload_file(str(path), settings.s3_bucket, key)

    return upload


def s3_remover(settings: Any) -> Optional[Callable[[str], None]]:
    if not getattr(settings, "s3_bucket", None):
        return None

    def remove(key: str) -> None:
        import boto3

        client = boto3.client("s3", endpoint_url=settings.s3_endpoint or None,
                              aws_access_key_id=settings.s3_access_key_id or None,
                              aws_secret_access_key=settings.s3_secret_access_key or None,
                              region_name=settings.s3_region or None)
        client.delete_object(Bucket=settings.s3_bucket, Key=key)

    return remove


def run_backup(engine: Any, settings: Any = None, *, now: Optional[datetime] = None,
               runner: Runner = subprocess.run, kind: str = "scheduled",
               uploader: Optional[Callable[[Path, str], None]] = None) -> dict[str, Any]:
    """Dump, verify, (upload), record. Returns the row as written."""
    settings = _settings(settings)
    now = now or datetime.now(timezone.utc)
    backup_id = f"bkp_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    folder = Path(getattr(settings, "backup_dir", "/var/lib/boss/backups"))
    final = folder / f"boss-{backup_id}.dump"
    with engine.begin() as c:
        c.execute(text("INSERT INTO backups (id, kind, path, started_at, status) "
                       "VALUES (:i, :k, :p, :t, 'running')"),
                  {"i": backup_id, "k": kind, "p": str(final), "t": now})
    row: dict[str, Any] = {"id": backup_id, "kind": kind, "path": str(final), "status": "failed",
                           "bytes": None, "checksum": None, "object_key": None,
                           "verified_at": None, "error": None}
    partial = final.with_suffix(".dump.partial")
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        old_umask = os.umask(0o077)
        try:
            done = runner([getattr(settings, "backup_pg_dump", "pg_dump"), "-Fc", "--no-password",
                           "-f", str(partial)], env=_pg_env(settings.database_url),
                          capture_output=True, text=True, timeout=DUMP_TIMEOUT_SECONDS)
        finally:
            os.umask(old_umask)
        if done.returncode != 0:
            raise RuntimeError(f"pg_dump failed ({done.returncode}): {_short(done.stderr)}")
        if not partial.exists() or partial.stat().st_size == 0:
            raise RuntimeError("pg_dump wrote nothing")
        partial.replace(final)
        row["bytes"] = final.stat().st_size
        row["checksum"] = "sha256:" + _sha256(final)
        problem = verify(final, settings, runner)
        if problem:
            raise RuntimeError(problem)
        row["verified_at"] = datetime.now(timezone.utc)
        row["status"] = "verified"
        if uploader is not None:
            key = f"{getattr(settings, 'backup_s3_prefix', 'boss-backups').strip('/')}/{final.name}"
            try:
                uploader(final, key)
                row["object_key"] = key
            except Exception as exc:  # noqa: BLE001 — the local copy is still good
                row["error"] = f"upload failed: {type(exc).__name__}: {_short(exc)}"
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        row["status"] = "failed"
        row["error"] = _short(exc) if isinstance(exc, RuntimeError) else \
            f"{type(exc).__name__}: {_short(exc)}"
        for leftover in (partial, final):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _log.warning("could not remove a failed backup file", exc_info=True)
    with engine.begin() as c:
        c.execute(text("UPDATE backups SET status = :s, bytes = :b, checksum = :ck, "
                       "object_key = :ok, verified_at = :v, error = :e, finished_at = now() "
                       "WHERE id = :i"),
                  {"s": row["status"], "b": row["bytes"], "ck": row["checksum"],
                   "ok": row["object_key"], "v": row["verified_at"], "e": row["error"],
                   "i": backup_id})
    if row["status"] == "failed":
        _log.warning("scheduled backup %s failed: %s", backup_id, row["error"])
    return row


def prune(engine: Any, settings: Any = None, *,
          remover: Optional[Callable[[str], None]] = None) -> list[str]:
    """Apply the tiers to verified backups: remove the rest (file + object), mark them pruned."""
    settings = _settings(settings)
    with engine.begin() as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT id, path, object_key, started_at FROM backups WHERE status = 'verified'"
        )).mappings()]
    keep = retention_keep(rows, Policy.from_settings(settings))
    pruned: list[str] = []
    for row in rows:
        if str(row["id"]) in keep:
            continue
        if row.get("path"):
            try:
                Path(row["path"]).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _log.warning("could not remove pruned backup %s", row["id"], exc_info=True)
                continue
        if row.get("object_key") and remover is not None:
            try:
                remover(str(row["object_key"]))
            except Exception:  # noqa: BLE001 — the row stays pruned; the object is the store's
                _log.warning("could not remove uploaded backup %s", row["id"], exc_info=True)
        with engine.begin() as c:
            c.execute(text("UPDATE backups SET status = 'pruned' WHERE id = :i"),
                      {"i": row["id"]})
        pruned.append(str(row["id"]))
    return pruned


# ------------------------------------------------------------------------------------------ #
# health — what the system doctor reads
# ------------------------------------------------------------------------------------------ #

def health(connection: Any, settings: Any = None, *,
           now: Optional[datetime] = None) -> dict[str, Any]:
    settings = _settings(settings)
    now = now or datetime.now(timezone.utc)
    if not getattr(settings, "backup_schedule_enabled", False):
        return {"state": "disabled"}
    interval = _interval(settings)
    last = connection.execute(text(
        "SELECT id, status, started_at, finished_at, verified_at, error FROM backups "
        "WHERE kind = 'scheduled' AND status <> 'running' "
        "ORDER BY started_at DESC LIMIT 1")).mappings().first()
    good = connection.execute(text(
        "SELECT id, started_at, verified_at, error, object_key FROM backups "
        "WHERE status IN ('verified', 'pruned') AND verified_at IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1")).mappings().first()
    out: dict[str, Any] = {
        "interval_minutes": int(interval.total_seconds() // 60),
        "last_verified_at": good["verified_at"].isoformat() if good else None,
        "last_backup_id": (last or {}).get("id"),
    }
    if last is not None and last["status"] == "failed":
        return {**out, "state": "failed", "error": last["error"]}
    if good is None:
        lease = connection.execute(text(
            "SELECT first_seen_at FROM backup_scheduler_lease WHERE name = :n"),
            {"n": LEASE_NAME}).first()
        since = _utc(lease[0]) if lease else None
        if since is not None and now - since > 2 * interval + timedelta(minutes=10):
            return {**out, "state": "missing"}
        return {**out, "state": "pending"}
    age = now - _utc(good["started_at"])
    if age > 3 * interval:
        return {**out, "state": "stale", "age_minutes": int(age.total_seconds() // 60)}
    if good["error"] and str(good["error"]).startswith("upload failed") and \
            getattr(settings, "backup_s3_upload", False):
        return {**out, "state": "failed", "error": good["error"]}
    return {**out, "state": "ok"}


# ------------------------------------------------------------------------------------------ #
# the thread
# ------------------------------------------------------------------------------------------ #

def tick(engine: Any, settings: Any = None, *, now: Optional[datetime] = None,
         holder: str = HOLDER, runner: Runner = subprocess.run,
         uploader: Optional[Callable[[Path, str], None]] = None,
         remover: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    settings = _settings(settings)
    now = now or datetime.now(timezone.utc)
    if not getattr(settings, "backup_schedule_enabled", False):
        return {"ran": False, "reason": "disabled"}
    with engine.begin() as c:
        if not acquire_lease(c, holder, now):
            return {"ran": False, "reason": "another process holds the lease"}
        mark_interrupted(c, now)
        due = is_due(c, settings, now)
    if not due:
        return {"ran": False, "reason": "not due"}
    row = run_backup(engine, settings, now=now, runner=runner,
                     uploader=uploader if uploader is not None else s3_uploader(settings))
    pruned = prune(engine, settings,
                   remover=remover if remover is not None else s3_remover(settings))
    return {"ran": True, "backup": row, "pruned": pruned}


_THREAD: Optional[threading.Thread] = None


def start(engine: Any, settings: Any = None, *, interval: float = TICK_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    settings = _settings(settings)

    def _loop() -> None:
        time.sleep(FIRST_TICK_DELAY)
        while True:
            try:
                report = tick(engine, settings)
                if report.get("ran"):
                    _log.info("scheduled backup: %s", report["backup"].get("status"))
            except Exception:  # noqa: BLE001 — a backup thread must never take the API down
                _log.warning("backup tick failed", exc_info=True)
            time.sleep(interval)

    _THREAD = threading.Thread(target=_loop, name="boss-backup-scheduler", daemon=True)
    _THREAD.start()
    return _THREAD
