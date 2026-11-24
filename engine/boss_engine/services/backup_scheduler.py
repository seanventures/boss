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

import contextlib
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


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=str)

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
    keep_hourly: int = 6
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6

    @classmethod
    def from_settings(cls, settings: Any) -> "Policy":
        return cls(keep_hourly=int(getattr(settings, "backup_keep_hourly", 6)),
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
        # ST-6 LABELS (BOSSAPP-1151): every backup BOSS writes says who made it, what kind it is
        # and how long to keep it, so the Infra Agent's cleanup touches labelled things only.
        c.execute(text("INSERT INTO backups (id, kind, path, started_at, status, label, retain_until) "
                       "VALUES (:i, :k, :p, :t, 'running', CAST(:l AS JSONB), "
                       ":t + make_interval(days => :keep))"),
                  {"i": backup_id, "k": kind, "p": str(final), "t": now,
                   "l": _json_dumps({"boss.owner": "backup_scheduler", "boss.kind": f"backup.{kind}",
                                     "boss.install_id": os.environ.get("BOSS_INSTALL_ID", ""),
                                     "boss.ttl_days": int(getattr(settings, "backup_keep_daily", 7) or 7)}),
                   "keep": int(getattr(settings, "backup_keep_daily", 7) or 7)})
    row: dict[str, Any] = {"id": backup_id, "kind": kind, "path": str(final), "status": "failed",
                           "bytes": None, "checksum": None, "object_key": None,
                           "verified_at": None, "error": None}
    partial = final.with_suffix(".dump.partial")
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        # OWNER-ONLY BY THE FILE'S OWN MODE, NEVER THE PROCESS UMASK. os.umask is process-wide:
        # for the ~35 s of this dump every file ANY thread of the API created was 0600, and a
        # review seat's `git status` rewrote a run's .git/index as boss 0600 (crun_6883ec7c lost
        # a finished build to it). The partial file is made 0600 first; pg_dump -f truncates
        # it and keeps that mode.
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.chmod(partial, 0o600)
        done = runner([getattr(settings, "backup_pg_dump", "pg_dump"), "-Fc", "--no-password",
                       "-f", str(partial)], env=_pg_env(settings.database_url),
                      capture_output=True, text=True, timeout=DUMP_TIMEOUT_SECONDS)
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


def _remove(engine: Any, row: Mapping[str, Any], remover: Optional[Callable[[str], None]],
            *, why: str = "pruned") -> bool:
    """One verified backup off the disk (file, then uploaded object), its row marked pruned."""
    if row.get("path"):
        try:
            Path(row["path"]).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _log.warning("could not remove %s backup %s", why, row["id"], exc_info=True)
            return False
    if row.get("object_key") and remover is not None:
        try:
            remover(str(row["object_key"]))
        except Exception:  # noqa: BLE001 — the row stays pruned; the object is the store's
            _log.warning("could not remove uploaded backup %s", row["id"], exc_info=True)
    with engine.begin() as c:
        c.execute(text("UPDATE backups SET status = 'pruned' WHERE id = :i AND status = 'verified'"),
                  {"i": row["id"]})
    return True


def prune(engine: Any, settings: Any = None, *,
          remover: Optional[Callable[[str], None]] = None) -> list[str]:
    """Apply the tiers to verified backups: remove the rest (file + object), mark them pruned.
    Then the cap (BOSSAPP-1459): past the count or the size the oldest go — at once when a
    `backups` rule is approved, else through one storage decision (`ask_over_cap`)."""
    settings = _settings(settings)
    with engine.begin() as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT id, path, object_key, started_at, bytes FROM backups WHERE status = 'verified'"
        )).mappings()]
    keep = retention_keep(rows, Policy.from_settings(settings))
    pruned: list[str] = []
    for row in rows:
        if str(row["id"]) in keep:
            continue
        if _remove(engine, row, remover):
            pruned.append(str(row["id"]))
    kept = [r for r in rows if str(r["id"]) in keep]
    from ..db import background

    with background():
        with engine.begin() as c:
            max_count, cap_gb, approved = cap_policy(c, settings)
        over = over_cap(kept, max_count=max_count, cap_gb=cap_gb)
        if over and approved:
            for row in over:
                if _remove(engine, row, remover, why="over-cap"):
                    pruned.append(str(row["id"]))
        elif over:
            ask_over_cap(engine, over, max_count=max_count, cap_gb=cap_gb,
                         kept_n=len(kept) - len(over))
    return pruned


# ------------------------------------------------------------------------------------------ #
# the cap — count and bytes over the tiers (BOSSAPP-1459)
# ------------------------------------------------------------------------------------------ #

#: The storage decision's tool name (storage_cleanup routes its answer here).
CAP_TOOL = "storage.remove_scheduled_backups"
#: Its OWN retention rule kind: never `backups`, which governs the host's pre-upgrade recovery
#: archives (storage.remove_backups) — approving this card must not make deleting those automatic.
CAP_RULE_KIND = "scheduled_backups"
#: The newest backups the cap never takes, whatever the rule says.
CAP_FLOOR = 3
#: A "Not now" is asked again after this long.
ASK_AGAIN_AFTER = timedelta(hours=24)


def cap_policy(connection: Any, settings: Any) -> tuple[int, float, bool]:
    """(max count, cap GB, approved). An APPROVED `scheduled_backups` retention rule (an
    operator's, or one learned from an answered card) sets both numbers and makes removal automatic; without one
    the settings give the numbers and the removal is asked (production) — or answered by the
    install's own autonomy (dev tier: autonomy_gate lets BOSS answer `storage_delete`)."""
    max_count = int(getattr(settings, "backup_max_count", 12) or 12)
    cap_gb = float(getattr(settings, "backup_cap_gb", 5.0) or 5.0)
    try:
        from . import storage_rules

        with connection.begin_nested():
            rule = storage_rules.rule_for(connection, CAP_RULE_KIND)
    except Exception:  # noqa: BLE001 — no rules table: the settings, asked
        rule = None
    if rule and rule.get("source") in ("operator", "learned"):
        r = rule.get("rule") or {}
        try:
            max_count = int(r.get("keep_newest") or max_count)
            cap_gb = float(r.get("cap_gb") or cap_gb)
        except (TypeError, ValueError):
            pass
        return max(CAP_FLOOR, max_count), cap_gb, True
    return max(CAP_FLOOR, max_count), cap_gb, False


def over_cap(rows: Iterable[Mapping[str, Any]], *, max_count: int, cap_gb: float,
             floor: int = CAP_FLOOR) -> list[dict[str, Any]]:
    """The oldest backups past `max_count` or past `cap_gb` in total — never one of the newest
    `floor`. Pure: newest first, a backup stays while both numbers still hold."""
    ordered = sorted((dict(r) for r in rows), key=lambda r: _utc(r["started_at"]), reverse=True)
    cap = cap_gb * 1e9
    used, out, cut = 0.0, [], False
    for i, row in enumerate(ordered):
        size = float(row.get("bytes") or 0)
        if i < floor or (not cut and i < max_count and used + size <= cap):
            used += size
            continue
        cut = True          # monotone: once one is past the cap, every older one is too
        out.append(row)
    return out


def ask_over_cap(engine: Any, over: list[Mapping[str, Any]], *, max_count: int,
                 cap_gb: float, kept_n: int = 0) -> Optional[str]:
    """ONE storage decision for the backups past the cap (the Infra Agent's card, kind
    `storage_delete`): it lists them, proposes the rule that makes it automatic next time, and is
    not raised again while one is open or for a day after a "Not now"."""
    import json

    from . import storage_cleanup, storage_incidents, storage_watch
    from .core import create_blocking_decision
    from .decision_actions import OPEN_STATUSES
    from .typed_forms import normalize_form

    gb = sum(float(r.get("bytes") or 0) for r in over) / 1e9
    with engine.begin() as c:
        recent = c.execute(text(
            "SELECT id FROM decisions WHERE recommendation->>'tool' = :t "
            "AND (status = ANY(:open) "
            "OR (status = 'rejected' AND updated_at > now() - make_interval(hours => :h))) "
            "LIMIT 1"), {"t": CAP_TOOL, "open": sorted(OPEN_STATUSES),
                         "h": int(ASK_AGAIN_AFTER.total_seconds() // 3600)}).first()
        if recent is not None:
            return None
        project_id = storage_incidents.default_project(c)
        if not project_id:
            return None
        proposed = {"keep_newest": int(max_count), "cap_gb": float(cap_gb)}
        question = (f"Delete {len(over)} old database backups ({gb:.1f} GB)? "
                    f"The newest {max(CAP_FLOOR, kept_n)} stay.")
        reason = (f"Scheduled backups are past their cap ({max_count} backups or {cap_gb:g} GB). "
                  "Deleting a backup cannot be undone, so on a production install a person says yes "
                  "once on this card: tick the rule and next time it is automatic. "
                  + storage_cleanup._LEFT_ALONE_LINE)
        host_id = storage_watch.local_host_id()
        ids = [str(r["id"]) for r in over]
        rec = {"action": "storage_delete", "tool": CAP_TOOL, "incident_id": None,
               "host_id": host_id, "args": proposed,
               "plan_hash": storage_cleanup.plan_hash(CAP_TOOL, host_id, proposed, ids),
               "items": len(over), "gb": round(gb, 2), "keeps": int(max_count),
               "to_delete": ids[:500], "kept": [],
               "already_done_gb": 0.0, "free_pct_now": None, "target_pct": None, "paused_runs": 0,
               "rule_proposal": {"kind": CAP_RULE_KIND, "rule": proposed, "preticked": True},
               "alternatives": [], "never_automatic": ["unlabelled", "data volumes",
                                                       "non-scratch databases"],
               "left_alone": list(storage_cleanup.LEFT_ALONE), "ui": "StorageDeleteDecision"}
        decision_id = create_blocking_decision(c, project_id, None, question, rec,
                                               {"estimated_cost_usd": 0.0}, reason,
                                               risk_tier=3, default_action="hold")
        c.execute(text("UPDATE decisions SET form_schema = CAST(:f AS JSONB) WHERE id = :id"),
                  {"f": json.dumps(normalize_form(storage_cleanup.ASK_FORM)), "id": decision_id})
    return str(decision_id)


def on_cap_decision(connection: Any, decision: Mapping[str, Any], *, verdict: str, actor: str,
                    make_rule: Optional[bool] = None,
                    args: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The storage decision's answer (storage_cleanup.on_delete_decision routes it here).
    Approve: write the rule when asked (next time it is automatic), then remove exactly the
    listed backups that are still verified. Not now: asked again after ASK_AGAIN_AFTER."""
    import json

    rec = decision.get("recommendation") or {}
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    if verdict != "approve":
        return {"state": "refused", "ask_again_after_hours": ASK_AGAIN_AFTER.total_seconds() / 3600}
    proposal = rec.get("rule_proposal") or {}
    rule = {**dict(proposal.get("rule") or {}), **dict(args or {})}
    want_rule = bool(proposal.get("preticked")) if make_rule is None else bool(make_rule)
    if want_rule:
        from . import storage_rules

        storage_rules.upsert(connection, kind=CAP_RULE_KIND, rule=rule, source="learned", actor=actor,
                             decision_id=str(decision.get("id") or ""))
    ids = [str(i) for i in (rec.get("to_delete") or [])]
    if args:
        # "Keep 20 instead": only what is STILL past the cap with the person's numbers goes —
        # never a backup the card listed that the new numbers keep.
        current = [dict(r) for r in connection.execute(text(
            "SELECT id, started_at, bytes FROM backups WHERE status = 'verified'")).mappings()]
        try:
            still = {str(r["id"]) for r in over_cap(
                current, max_count=max(CAP_FLOOR, int(rule.get("keep_newest") or CAP_FLOOR)),
                cap_gb=float(rule.get("cap_gb") or 1e9))}
        except (TypeError, ValueError):
            still = set()
        ids = [i for i in ids if i in still]
    rows = [dict(r) for r in connection.execute(text(
        "SELECT id, path, object_key, started_at, bytes FROM backups "
        "WHERE status = 'verified' AND id = ANY(:ids)"), {"ids": ids}).mappings()]
    # Never one of the newest CAP_FLOOR, even if they moved since the card was raised.
    newest = {str(r[0]) for r in connection.execute(text(
        "SELECT id FROM backups WHERE status = 'verified' ORDER BY started_at DESC LIMIT :n"),
        {"n": CAP_FLOOR}).all()}
    remover = s3_remover(_settings(None))
    removed: list[str] = []
    for row in rows:
        if str(row["id"]) in newest:
            continue
        if row.get("path"):
            try:
                Path(row["path"]).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _log.warning("could not remove over-cap backup %s", row["id"], exc_info=True)
                continue
        if row.get("object_key") and remover is not None:
            with contextlib.suppress(Exception):
                remover(str(row["object_key"]))
        connection.execute(text("UPDATE backups SET status = 'pruned' WHERE id = :i"), {"i": row["id"]})
        removed.append(str(row["id"]))
    return {"state": "done", "removed": removed, "rule": want_rule}


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
