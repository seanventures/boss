"""Identity maintenance: the scheduled half of the cascade and of key hygiene. Owned by S3.

One pass (`tick`), every MAINTENANCE_SECONDS in the API process, under a database advisory lock so
two processes never run it at once:

  * grants past `expires_at` EXPIRE THROUGH THE CASCADE (grants.expire_due): their tokens die,
    egress credentials die, runs halt, schedules pause, queued jobs are cancelled (#14). Before this
    an expired grant only failed its next live check — nothing halted;
  * "for this run" grants of a WORKFLOW run end when the run ends (completed, failed, cancelled…),
    through the same cascade (#14); a code task's end at run end is runs.end_run's;
  * signing keys rotate when due (30 days) and retiring keys retire (signing_keys.rotate_if_due),
    and so do the apps' `oidc` keys, every org's (signing_keys.rotate_oidc_family_if_due, F6);
  * provider revocations that failed are retried, and shredded once they succeed (vault);
  * (the audit chain's rows from before the keyed chain are re-keyed ONCE, at boot, by
    `rekey_at_boot` — never on this timer (N4): a re-key rewrites hashes, so it runs only when
    the API starts, after the old chain verifies);
  * expired DPoP replay rows are purged;
  * hygiene.py: API key expiry events, reminders and the unused-key policy; the security log's
    retention; the egress log's daily partitions and 30 days; permissions dropped from the
    catalogue retired with an audit event.
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


#: A run's child work that has settled: a code task, a stack deployment.
SETTLED_CODE_TASK_STATUSES = ("completed", "failed", "blocked", "cancelled", "error", "rejected")
SETTLED_STACK_DEPLOY_STATUSES = ("healthy", "failed", "rejected")


def live_child_work_sql(run: str = "w.id") -> str:
    """SQL true while the workflow run `run` still has child work going: a code task it started
    (by finalize_ctx or by a step's output) or a stack deployment it planned, not yet settled."""
    tasks = "','".join(SETTLED_CODE_TASK_STATUSES)
    stacks = "','".join(SETTLED_STACK_DEPLOY_STATUSES)
    return (
        f"(EXISTS (SELECT 1 FROM code_task_runs c WHERE c.status NOT IN ('{tasks}') AND ("
        f"   c.finalize_ctx->>'workflow_run_id' = {run} OR c.id IN ("
        f"     SELECT s.output->>'code_task_run_id' FROM workflow_step_runs s"
        f"     WHERE s.workflow_run_id = {run})))"
        f" OR EXISTS (SELECT 1 FROM stack_deployments d WHERE d.workflow_run_id = {run}"
        f"   AND d.status NOT IN ('{stacks}')))")


def end_finished_workflow_grants(*, db: Any) -> list[str]:
    """Task grants bound to a workflow run (`run_wf:<id>`) whose run has ended AND whose child work
    has all settled.

    A run goes terminal on one branch while another still works: a deploy failed while More tests
    was still writing tests on the branch. Ending the grant 30 s later killed that live code task
    (its run token died with the grant), and every Retry after re-asked consent. The grant now
    ends with the last child: a code task or stack deployment the run started, once settled."""
    from sqlalchemy import text

    from . import grants

    if not db.execute(text("SELECT to_regclass('workflow_runs') IS NOT NULL")).scalar():
        return []
    children = db.execute(text(
        "SELECT to_regclass('code_task_runs') IS NOT NULL "
        "AND to_regclass('stack_deployments') IS NOT NULL "
        "AND to_regclass('workflow_step_runs') IS NOT NULL")).scalar()
    rows = db.execute(text(
        "SELECT g.task_run_id, g.org_id FROM identity_delegation_grants g JOIN workflow_runs w "
        "ON g.task_run_id = 'run_wf:' || w.id WHERE g.status = 'active' AND w.status = ANY(:t)"
        + (f" AND NOT {live_child_work_sql()}" if children else "")),
        {"t": list(TERMINAL_WORKFLOW_STATUSES)}).all()
    ended: list[str] = []
    for run_id, org in {(r[0], r[1]) for r in rows}:
        ended += grants.end_task_grants(run_id, db=db, org_id=org)
    return ended


def tick(engine: Any, *, now: Optional[datetime] = None, force: bool = False) -> dict[str, Any]:
    """One maintenance pass. Each step in its own transaction, so one failing never blocks the rest.
    `force`: run the hourly and daily hygiene steps now (tests, the host CLI)."""
    from sqlalchemy import text

    from . import grants, hygiene, vault

    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    # PER PROCESS, before the cross-process lock: the unscoped GitHub App tokens this process
    # minted live in its own memory, so no other process can revoke them (id-sec-final2).
    try:
        from .github_setup import sweep_control_plane_tokens

        out["control_plane_tokens"] = sweep_control_plane_tokens()
    except Exception as exc:  # noqa: BLE001 — logged; the next pass retries
        log.warning("identity maintenance step control_plane_tokens failed: %s", type(exc).__name__)
        out["control_plane_tokens"] = f"failed:{type(exc).__name__}"
    with engine.begin() as db:
        got = db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_KEY}).scalar()
        if not got:
            return {**out, "skipped": "another process holds the maintenance lock"}
        steps = [
            ("expired_grants", lambda c: grants.expire_due(db=c, now=now)),
            ("ended_workflow_grants", lambda c: end_finished_workflow_grants(db=c)),
            ("grant_reminders", lambda c: grants.remind_expiring(db=c, now=now)),
            ("rotated_keys", lambda c: _rotate(c)),
            # The `oidc` family (apps' ID/access tokens, F6; D35): its own table, every org.
            ("rotated_oidc_keys", lambda c: _rotate_oidc(c, now=now)),
            ("provider_revocations", lambda c: vault.retry_provider_revocations(db=c)),
            ("dpop_purged", lambda c: c.execute(text("DELETE FROM identity_dpop_replay WHERE expires_at < now()")).rowcount),
            ("retired_permissions", lambda c: _per_org(c, hygiene.retire_removed_permissions)),
            ("key_hygiene", lambda c: _per_org(c, hygiene.key_hygiene, now=now)
             if hygiene.due("key_hygiene", force=force) else "not due"),
            ("audit_retention", lambda c: _per_org(c, hygiene.audit_retention, now=now)
             if hygiene.due("audit_retention", force=force) else "not due"),
            ("egress_partitions", lambda c: hygiene.egress_partitions(c, now=now)
             if hygiene.due("egress_partitions", force=force) else "not due"),
        ]
        for name, step in steps:
            try:
                with db.begin_nested():
                    out[name] = step(db)
            except Exception as exc:  # noqa: BLE001 — logged; the next pass retries
                log.warning("identity maintenance step %s failed: %s", name, type(exc).__name__)
                out[name] = f"failed:{type(exc).__name__}"
    return out


def _per_org(db: Any, step: Any, **kw: Any) -> Any:
    from . import migration

    org = migration.default_org(db)
    return step(db, org_id=org, **kw) if org else None


def _rotate_oidc(db: Any, *, now: Optional[datetime] = None) -> list[str]:
    from sqlalchemy import text

    from . import signing_keys

    if not db.execute(text("SELECT to_regclass('oidc_signing_keys') IS NOT NULL")).scalar():
        return []
    return signing_keys.rotate_oidc_family_if_due(db=db, now=now)


def _rotate(db: Any) -> list[str]:
    from . import migration, signing_keys

    org = migration.default_org(db)
    return signing_keys.rotate_if_due(org_id=org, db=db) if org else []


def rekey_at_boot(engine: Any) -> int:
    """N4: the one-time re-key of pre-keyed audit rows, at API boot only (audit.rekey re-keys a
    chain only after its old hashes verify, and identity_audit_rekey only a whole chain)."""
    try:
        with engine.begin() as db:
            return _rekey(db)
    except Exception as exc:  # noqa: BLE001 — logged; the chain still verifies (or reports) as is
        log.warning("identity audit re-key failed: %s", type(exc).__name__)
        return 0


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
