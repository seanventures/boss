"""One warm sandbox per lineage, reused by fetch+reset instead of a fresh clone.

A repair round, retry or follow-up of the same lineage (`root_run_id`) claims the
lineage's sandbox row: if it is live and owned by the claiming owner, it is reused
(fetch+reset cost, not clone+install+DB-provision cost); otherwise it is provisioned
fresh. Purge is the one teardown path, fired on workflow end, idle-TTL expiry, or a
`sandbox.reuse.enabled` flip-off — never a parallel "lightweight" variant.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Illustrative costs for a lineage sandbox round, standing in for the real clone +
# dependency install + `CREATE DATABASE` (fresh) vs. `git fetch`+`reset --hard` (reused).
FRESH_PROVISION_SECONDS = 45.0
REUSE_PROVISION_SECONDS = 2.0

@dataclass
class LineageSandboxClaim:
    lineage_id: str
    run_dir: Optional[str]
    uid: int
    database_name: Optional[str]
    reused: bool
    provisioning_seconds: float
    stages_skipped: list = field(default_factory=list)
    denied: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir_for(lineage_id: str) -> str:
    return f"/var/lib/boss/lineage-sandboxes/{lineage_id}"


def _uid_for(owner: Any) -> int:
    return (abs(hash(str(owner))) % 50_000) + 1_000


def _record_violation(conn, lineage_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO sandbox_violation_counts (lineage_id, count) VALUES (:l, 1) "
            "ON CONFLICT(lineage_id) DO UPDATE SET count = count + 1"
        ),
        {"l": lineage_id},
    )


def violation_count(engine: Engine, *, lineage_id: str) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT count FROM sandbox_violation_counts WHERE lineage_id=:l"),
            {"l": lineage_id},
        ).first()
    return int(row[0]) if row else 0


def claim(
    engine: Engine,
    settings: Any,
    *,
    lineage_id: str,
    run_id: str,
    project_id: str,
    owner: Any,
) -> LineageSandboxClaim:
    """Claim the lineage's warm sandbox, or provision a fresh one.

    The select-then-act happens inside one transaction (`SELECT ... FOR UPDATE` on real
    Postgres; SQLite's implicit write-lock on `engine.begin()` gives the same single-claim
    guarantee for this engine), so a concurrent eviction or claim cannot observe a
    half-evicted row.
    """
    owner_uid = _uid_for(owner)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM lineage_sandboxes WHERE lineage_id=:l FOR UPDATE"
                 if engine.dialect.name != "sqlite" else
                 "SELECT * FROM lineage_sandboxes WHERE lineage_id=:l"),
            {"l": lineage_id},
        ).mappings().first()

        live = row is not None and row["purged_at"] is None
        if live:
            if int(row["uid"]) != owner_uid:
                _record_violation(conn, lineage_id)
                return LineageSandboxClaim(
                    lineage_id=lineage_id, run_dir=None, uid=owner_uid,
                    database_name=None, reused=False,
                    provisioning_seconds=0.0, denied=True,
                )
            conn.execute(
                text(
                    "UPDATE lineage_sandboxes SET current_run_id=:r, status='warm', "
                    "last_used_at=:t WHERE lineage_id=:l"
                ),
                {"r": run_id, "l": lineage_id, "t": _now()},
            )
            return LineageSandboxClaim(
                lineage_id=lineage_id, run_dir=row["run_dir"], uid=int(row["uid"]),
                database_name=row["database_name"], reused=True,
                provisioning_seconds=REUSE_PROVISION_SECONDS,
            )

        run_dir = _run_dir_for(lineage_id)
        database_name = f"lineage_{lineage_id}".replace("-", "_")
        now = _now()
        if row is None:
            conn.execute(
                text(
                    "INSERT INTO lineage_sandboxes "
                    "(lineage_id, project_id, run_dir, uid, database_name, status, "
                    "stage_hashes, current_run_id, created_at, last_used_at) "
                    "VALUES (:lineage_id, :project_id, :run_dir, :uid, :database_name, "
                    "'warm', '{}', :run_id, :now, :now)"
                ),
                {
                    "lineage_id": lineage_id, "project_id": project_id, "run_dir": run_dir,
                    "uid": owner_uid, "database_name": database_name, "run_id": run_id,
                    "now": now,
                },
            )
        else:
            conn.execute(
                text(
                    "UPDATE lineage_sandboxes SET project_id=:project_id, run_dir=:run_dir, "
                    "uid=:uid, database_name=:database_name, status='warm', "
                    "stage_hashes='{}', dependency_manifest_hash=NULL, current_run_id=:run_id, "
                    "created_at=:now, last_used_at=:now, purged_at=NULL, release_reason=NULL "
                    "WHERE lineage_id=:lineage_id"
                ),
                {
                    "lineage_id": lineage_id, "project_id": project_id, "run_dir": run_dir,
                    "uid": owner_uid, "database_name": database_name, "run_id": run_id,
                    "now": now,
                },
            )
        return LineageSandboxClaim(
            lineage_id=lineage_id, run_dir=run_dir, uid=owner_uid,
            database_name=database_name, reused=False,
            provisioning_seconds=FRESH_PROVISION_SECONDS,
        )


def purge(engine: Engine, settings: Any, *, lineage_id: str, reason: str) -> None:
    """The one teardown path, used on workflow end, idle-TTL expiry, cancellation and a
    `sandbox.reuse.enabled` flip-off alike."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE lineage_sandboxes SET status='purging', purged_at=:t, "
                "release_reason=:reason WHERE lineage_id=:l"
            ),
            {"t": _now(), "reason": reason, "l": lineage_id},
        )


def release(engine: Engine, settings: Any, *, lineage_id: str, keep_warm: bool,
            reason: Optional[str] = None) -> None:
    if not keep_warm:
        purge(engine, settings, lineage_id=lineage_id, reason=reason or "workflow_end")


def evict_idle(engine: Engine, settings: Any) -> list[str]:
    """Purge lineage sandboxes that have sat warm, unclaimed, past the idle TTL."""
    ttl_hours = getattr(settings, "sandbox_idle_ttl_hours", 6)
    purged: list[str] = []
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT lineage_id, last_used_at FROM lineage_sandboxes "
                "WHERE purged_at IS NULL AND status = 'warm'"
            )
        ).mappings().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        last_used = row["last_used_at"]
        try:
            last_used_dt = datetime.fromisoformat(str(last_used))
        except ValueError:
            continue
        if last_used_dt.tzinfo is None:
            last_used_dt = last_used_dt.replace(tzinfo=timezone.utc)
        idle_hours = (now - last_used_dt).total_seconds() / 3600.0
        if idle_hours >= ttl_hours:
            purge(engine, settings, lineage_id=row["lineage_id"], reason="ttl")
            purged.append(row["lineage_id"])
    return purged


def _hash_stage_input(brief_text: str, steer_notes: Sequence[str]) -> str:
    data = brief_text + "\n".join(steer_notes)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def stage_needs_rerun(*, recorded_hash: Optional[str], brief_text: str,
                       steer_notes: Sequence[str]) -> bool:
    """Whether a stage already in `resume_completed_stages` must still re-run.

    No steer notes since the stage last passed means nothing new has been said about
    scope, so the stage stays carried forward. Any new steer note changes the input hash
    `hash(brief_text + steer_notes)`, and a changed hash means re-run even though the
    stage is in the resumed set [acceptance criterion 3].
    """
    if not steer_notes:
        return False
    current_hash = _hash_stage_input(brief_text, steer_notes)
    return current_hash != recorded_hash
