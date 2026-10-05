"""A DEPLOY DRAINS; IT DOES NOT KILL BUILDS (stability spec §4a.5, C6).

Measured on boss-1: one code task went through 3 hand-backs and 4 lease reclaims in 52
minutes because every deploy SIGKILLed the API 10 s after SIGTERM (exit 137), mid agent turn,
and each restart redid clone, install and agent turns. To the operator it looked like random
failures.

Now, on SIGTERM:

1. stop admitting — `jobs.claim_job` leases nothing while draining, and no new workload
   process is spawned (owned_process refuses);
2. mark every build running here `interrupted_by_deploy` (an event and a stamp on the run);
3. end every owned process tree, so nothing keeps writing while the container goes away;
4. each executor whose stage died of it hands its job back to the queue WITHOUT spending an
   attempt, and the next API picks it up.

compose gives the API 60 s for this (`stop_grace_period`). And `deploy_readiness` lets a deploy
script wait for running builds to finish first when the operator chose "Hold updates while
builds run" on the system-health card.
"""
from __future__ import annotations

import hashlib
import json
import logging
import signal
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from . import owned_process

_log = logging.getLogger("boss.drain")

#: Seconds given to the trees to leave on SIGTERM before SIGKILL, inside the 60 s grace.
TREE_GRACE_SECONDS = 5.0


def hand_back_interrupted(engine: Any, jobs: Any, run_id: str, project_id: Optional[str],
                          fencing_token: Optional[str]) -> dict[str, Any]:
    """Give the run's leased job back to the queue (no attempt spent) and say why."""
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with engine.begin() as c:
            c.execute(text(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                "|| jsonb_build_object('interrupted_by_deploy', CAST(:t AS text)) "
                "WHERE id = :id"), {"t": stamp, "id": run_id})
            if fencing_token and hasattr(jobs, "defer_leased"):
                job = c.execute(text(
                    "SELECT id, lease_owner FROM jobs WHERE kind = 'code.task' "
                    "AND payload->>'run_id' = :r AND lease_token_hash = :h "
                    "AND status IN ('leased', 'running') LIMIT 1"),
                    {"r": run_id,
                     "h": hashlib.sha256(fencing_token.encode("utf-8")).hexdigest()},
                ).mappings().first()
                if job is not None:
                    jobs.defer_leased(c, job["lease_owner"], job["id"], fencing_token,
                                      delay_seconds=5, reason="interrupted_by_deploy",
                                      message="BOSS restarted for an update; the build resumes")
    except Exception:  # noqa: BLE001 — the lease expires and is reclaimed either way
        _log.warning("could not hand back the job of %s while draining", run_id, exc_info=True)
    # Never a failure: either the job is queued again, or its lease runs out and is reclaimed.
    return {"status": "running", "done": False, "job_deferred": True,
            "error": "interrupted by a BOSS update; it resumes when BOSS is back"}


def drain(engine: Any, settings: Any = None) -> dict[str, Any]:
    """Steps 1-3 above. Idempotent; returns what it did."""
    owned_process.begin_drain()
    candidates = sorted(owned_process.live_owner_ids())
    runs: list[str] = []
    try:
        with engine.begin() as c:
            runs = sorted(str(r[0]) for r in c.execute(text(
                "SELECT id FROM code_task_runs WHERE id = ANY(:ids)"), {"ids": candidates}).all())
    except Exception:  # noqa: BLE001 — unknown: end the trees anyway
        runs = []
    project = getattr(settings, "default_project_id", None) if settings is not None else None
    for run_id in runs:
        try:
            with engine.begin() as c:
                row = c.execute(text("SELECT project_id FROM code_task_runs WHERE id = :id"),
                                {"id": run_id}).mappings().first()
                pid = (row or {}).get("project_id") or project
                if pid:
                    from .core import emit_event
                    emit_event(c, pid, "code_task.interrupted_by_deploy", "boss",
                               "code_task_run", run_id,
                               {"reason": "the API is stopping for an update"})
        except Exception:  # noqa: BLE001
            _log.debug("could not record the interruption of %s", run_id, exc_info=True)
    ended = owned_process.terminate_all(reason="deploy", grace=TREE_GRACE_SECONDS)
    _log.warning("drained for shutdown: %d build(s) interrupted, %d process tree(s) ended",
                 len(runs), ended)
    return {"interrupted_runs": runs, "trees_ended": ended}


def install_sigterm_drain(engine: Any, settings: Any = None) -> bool:
    """Run `drain` the moment SIGTERM arrives, then hand the signal to whoever had it (uvicorn).

    Uvicorn's own shutdown waits for in-flight requests — and a code task IS an in-flight
    request, for hours — so a drain placed after the lifespan's `yield` would never run
    before Docker's SIGKILL. Must be called from the main thread (the lifespan is)."""
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        return False

    def _on_term(signum: int, frame: Any) -> None:
        threading.Thread(target=drain, args=(engine, settings), name="boss-drain",
                         daemon=True).start()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        return False
    return True


def deploy_readiness(connection: Any) -> dict[str, Any]:
    """Whether a deploy now would interrupt builds, and whether the operator asked to wait."""
    running = connection.execute(text(
        "SELECT count(*) FROM code_task_runs WHERE status IN ('running', 'queued')")).scalar()
    hold = False
    try:
        val = connection.execute(text(
            "SELECT value FROM system_state WHERE key = 'deploy_hold'")).scalar()
        if isinstance(val, str):
            val = json.loads(val)
        hold = bool((val or {}).get("hold"))
    except Exception:  # noqa: BLE001
        hold = False
    running = int(running or 0)
    return {"running_builds": running, "hold_while_building": hold,
            "safe_to_deploy": running == 0 or not hold,
            "message": ("No builds are running." if running == 0 else
                        f"{running} build(s) running; " + (
                            "the operator asked to hold updates until they finish."
                            if hold else "an update now interrupts them; they resume after."))}
