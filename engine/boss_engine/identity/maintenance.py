"""Identity maintenance: the scheduled half of the cascade and of key hygiene. Owned by S3.

One pass (`tick`), every MAINTENANCE_SECONDS in the API process, under a database advisory lock so
two processes never run it at once:

  * grants past `expires_at` EXPIRE THROUGH THE CASCADE (grants.expire_due): their tokens die,
    egress credentials die, runs halt, schedules pause, queued jobs are cancelled (#14). Before this
    an expired grant only failed its next live check — nothing halted;
  * "for this run" grants of a WORKFLOW run end when the run ends (completed, failed, cancelled…),
    through the same cascade (#14); a code task's end at run end is runs.end_run's;
  * signing keys rotate when due (30 days) and retiring keys retire (signing_keys.rotate_if_due);
  * provider revocations that failed are retried, and shredded once they succeed (vault);
  * the audit chain's rows from before the keyed chain are re-keyed once (audit.rekey);
  * expired DPoP replay rows are purged.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("boss.identity.maintenance")

MAINTENANCE_SECONDS = 30
_LOCK_KEY = 0x6964_6D61_696E      # "idmain"
TERMINAL_WORKFLOW_STATUSES = ("completed", "failed", "deadlettered", "cancelled", "compensated")


def end_finished_workflow_grants(*, db: Any) -> list[str]:
    """Task grants bound to a workflow run (`run_wf:<id>`) whose run has ended."""
    from sqlalchemy import text

    from . import grants

    if not db.execute(text("SELECT to_regclass('workflow_runs') IS NOT NULL")).scalar():
        return []
    rows = db.execute(text(
        "SELECT g.task_run_id, g.org_id FROM identity_delegation_grants g JOIN workflow_runs w "
        "ON g.task_run_id = 'run_wf:' || w.id WHERE g.status = 'active' AND w.status = ANY(:t)"),
        {"t": list(TERMINAL_WORKFLOW_STATUSES)}).all()
    ended: list[str] = []
    for run_id, org in {(r[0], r[1]) for r in rows}:
        ended += grants.end_task_grants(run_id, db=db, org_id=org)
    return ended


def tick(engine: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """One maintenance pass. Each step in its own transaction, so one failing never blocks the rest."""
    from sqlalchemy import text

    from . import grants, vault

    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    with engine.begin() as db:
        got = db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_KEY}).scalar()
        if not got:
            return {"skipped": "another process holds the maintenance lock"}
        steps = [
            ("expired_grants", lambda c: grants.expire_due(db=c, now=now)),
            ("ended_workflow_grants", lambda c: end_finished_workflow_grants(db=c)),
            ("rotated_keys", lambda c: _rotate(c)),
            ("provider_revocations", lambda c: vault.retry_provider_revocations(db=c)),
            ("audit_rekeyed", lambda c: _rekey(c)),
            ("dpop_purged", lambda c: c.execute(text("DELETE FROM identity_dpop_replay WHERE expires_at < now()")).rowcount),
        ]
        for name, step in steps:
            try:
                with db.begin_nested():
                    out[name] = step(db)
            except Exception as exc:  # noqa: BLE001 — logged; the next pass retries
                log.warning("identity maintenance step %s failed: %s", name, type(exc).__name__)
                out[name] = f"failed:{type(exc).__name__}"
    return out


def _rotate(db: Any) -> list[str]:
    from . import migration, signing_keys

    org = migration.default_org(db)
    return signing_keys.rotate_if_due(org_id=org, db=db) if org else []


def _rekey(db: Any) -> int:
    from sqlalchemy import text

    from . import audit

    total = 0
    for org in db.execute(text("SELECT DISTINCT org_id FROM identity_security_events WHERE hash_alg = 1")).scalars().all():
        total += audit.rekey(org, db=db)
    return total


_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()


def start(engine: Any) -> None:
    """Start the maintenance loop (idempotent). The API calls this at boot."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()

    def loop() -> None:
        while not _STOP.wait(MAINTENANCE_SECONDS):
            try:
                tick(engine)
            except Exception:  # noqa: BLE001
                log.warning("identity maintenance pass failed", exc_info=True)

    _THREAD = threading.Thread(target=loop, daemon=True, name="identity-maintenance")
    _THREAD.start()


def stop() -> None:
    _STOP.set()
