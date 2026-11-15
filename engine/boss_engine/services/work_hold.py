"""THE INSTALL-WIDE WORK HOLD (Infra Agent, BOSSAPP-1157; boss-infra-agent-lld.md §4.2).

WHY. BOSS had a switch per subsystem — a work tree's `run_holds`, a trigger's pause, the ambient
stop — and none for "no new work anywhere until the disk is safe". The system doctor's only
"pause" was a cancel. Cleaning a disk while builds write to it is a race BOSS loses, so one
root switch is read by EVERY claimer:

    system_state['infra_hold'] = {"active": true, "incident_id": "si_…", "host_id": "…",
                                  "since": "…", "scope": "all",
                                  "exempt_kinds": ["agent.turn.interactive", "infra.*"],
                                  "actor": "agent:infra"}

Readers (one helper, `active()`, cached for five seconds per process):
  1. jobs.claim_job          — a non-exempt job stays queued, stamped last_error.code='infra_hold'
                               so the console says "paused by Infra Agent"; no attempt is spent.
  2. workflows.claim_step    — a run claims nothing while the hold is on.
  3. agent_triggers.tick     — a clocked trigger is not fired and its next_fire_at is not
                               advanced, so nothing is lost; it fires on the first tick after.
  4. ambient guards.admit_start — refused with guard 'infra_hold'.
  5. admission.admit_job     — refused with code 'infra_hold'.

Running work is NEVER killed. A kernel run and a code-task build check the hold at their next
checkpoint / stage boundary and hand their job back through `jobs.defer_leased` (no attempt
spent, the work keeps its fence epoch) — exactly the deploy drain's path with a different
reason. Reclaim after release bumps `fence_epoch` (BOSSAPP-986), so a stale writer from the
paused lease is fenced out: nothing lost, nothing duplicated.

Exempt: interactive chat turns (threads on surface 'chat'), the Infra Agent's own runs, and
anything matching `exempt_kinds` (fnmatch). A stuck hold auto-releases (storage_watch.tick)
when its incident is resolved or no sample has arrived for 15 minutes and the last known
reading was above target.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.work_hold")

KEY = "infra_hold"
REASON = "infra_hold"
PAUSED_BY = "infra_agent"
DEFAULT_EXEMPT_KINDS: tuple[str, ...] = ("agent.turn.interactive", "infra.*")
CACHE_SECONDS = 5.0
#: How long a handed-back job waits before claim_job may look at it again; claim_job refuses
#: it anyway while the hold is on, and release sets run_after = now(), so this only bounds a
#: hold that vanished without a release.
HANDED_BACK_DELAY_SECONDS = 3600

_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def invalidate_cache() -> None:
    _CACHE["at"] = 0.0
    _CACHE["value"] = None


def state(connection: Any, *, fresh: bool = False) -> dict[str, Any]:
    """The hold row as stored ({} when none). Cached for CACHE_SECONDS unless `fresh`."""
    now = time.monotonic()
    if not fresh and _CACHE["value"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
        return dict(_CACHE["value"])
    try:
        raw = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                                 {"k": KEY}).scalar()
    except Exception:  # noqa: BLE001 — an older schema has no system_state: nothing is held
        raw = None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    value = raw if isinstance(raw, dict) else {}
    _CACHE["at"] = now
    _CACHE["value"] = dict(value)
    return dict(value)


def active(connection: Any, kind: Optional[str] = None, *, fresh: bool = False) -> bool:
    """Is the hold on — and, with `kind`, does it apply to work of that kind?"""
    s = state(connection, fresh=fresh)
    if not s.get("active"):
        return False
    if kind is not None and exempt_kind(kind, s.get("exempt_kinds")):
        return False
    return True


def exempt_kind(kind: str, exempt_kinds: Optional[Any] = None) -> bool:
    patterns = [str(p) for p in (exempt_kinds or DEFAULT_EXEMPT_KINDS)]
    return any(fnmatch.fnmatchcase(str(kind), p) for p in patterns)


def exempt_job(connection: Any, candidate: Mapping[str, Any],
               hold: Optional[Mapping[str, Any]] = None) -> bool:
    """Is this job exempt from the hold? A kind in `exempt_kinds`; an agent run on a chat
    thread (interactive: a person is waiting and it needs no disk); the Infra Agent's own run."""
    hold = hold if hold is not None else state(connection)
    kind = str(candidate.get("kind") or "")
    if exempt_kind(kind, hold.get("exempt_kinds")):
        return True
    if kind in ("agent.run", "agent.resume"):
        payload = candidate.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        run_id = (payload or {}).get("run_id")
        if run_id:
            try:
                row = connection.execute(text(
                    "SELECT r.agent_key, t.surface FROM agent_runs r "
                    "LEFT JOIN agent_threads t ON t.id = r.thread_id WHERE r.id = :r"),
                    {"r": run_id}).mappings().first()
            except Exception:  # noqa: BLE001 — unknown is not exempt
                row = None
            if row is not None:
                if str(row.get("agent_key") or "") == "infra_agent":
                    return True
                if str(row.get("surface") or "") == "chat" and exempt_kind(
                        "agent.turn.interactive", hold.get("exempt_kinds")):
                    return True
    return False


def set_hold(connection: Any, *, incident_id: str, host_id: str, actor: str = "agent:infra",
             project_id: Optional[str] = None, exempt_kinds: Optional[list[str]] = None,
             scope: str = "all") -> dict[str, Any]:
    """Turn the hold on (idempotent: an active hold for the same incident is left as is)."""
    current = state(connection, fresh=True)
    if current.get("active") and current.get("incident_id") == incident_id:
        return current
    value = {"active": True, "incident_id": incident_id, "host_id": host_id,
             "since": _iso(_now()), "scope": scope,
             "exempt_kinds": list(exempt_kinds or DEFAULT_EXEMPT_KINDS), "actor": actor}
    connection.execute(text(
        "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": KEY, "v": json.dumps(value)})
    invalidate_cache()
    _emit(connection, project_id, "infra.hold.set", actor, incident_id,
          {"incident_id": incident_id, "host_id": host_id, "scope": scope})
    return value


def release(connection: Any, *, actor: str, reason: str = "",
            project_id: Optional[str] = None) -> dict[str, Any]:
    """Clear the hold and let paused jobs go, oldest first. Returns {"hold", "resumed_jobs"}."""
    current = state(connection, fresh=True)
    incident_id = current.get("incident_id")
    released_at = _iso(_now())
    value = {**current, "active": False, "released_at": released_at, "released_by": actor,
             "release_reason": (reason or "")[:300]}
    connection.execute(text(
        "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": KEY, "v": json.dumps(value)})
    invalidate_cache()
    # Oldest first: run_after = now() plus a few milliseconds per rank keeps the claim order
    # the LLD asks for without touching priority.
    resumed = connection.execute(text("""
        WITH ranked AS (
          SELECT id, row_number() OVER (ORDER BY created_at, id) AS n FROM jobs
           WHERE status IN ('queued', 'failed_retryable') AND last_error->>'code' = :code)
        UPDATE jobs j SET run_after = now() + make_interval(secs => (ranked.n - 1) * 0.01),
               last_error = COALESCE(j.last_error, '{}'::jsonb)
                            || jsonb_build_object('resumed_at', CAST(:at AS text)),
               updated_at = now()
          FROM ranked WHERE j.id = ranked.id RETURNING j.id"""),
        {"code": REASON, "at": released_at}).scalars().all()
    if current.get("active"):
        _emit(connection, project_id, "infra.hold.released", actor, incident_id,
              {"incident_id": incident_id, "resumed_jobs": len(resumed),
               "reason": (reason or "")[:300]})
    return {"hold": status(connection), "resumed_jobs": len(resumed)}


def paused_jobs(connection: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    """Jobs the hold is keeping: queued with last_error.code='infra_hold' (claim refused or
    handed back at a checkpoint), oldest first."""
    rows = connection.execute(text(
        "SELECT id, kind, project_id, task_id, payload, created_at, updated_at, last_error "
        "FROM jobs WHERE status IN ('queued', 'failed_retryable') "
        "AND last_error->>'code' = :code ORDER BY created_at LIMIT :n"),
        {"code": REASON, "n": int(limit)}).mappings().all()
    out = []
    for r in rows:
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        err = r.get("last_error") or {}
        if isinstance(err, str):
            try:
                err = json.loads(err)
            except ValueError:
                err = {}
        out.append({"job_id": r["id"], "kind": r["kind"], "project_id": r["project_id"],
                    "run_id": (payload or {}).get("run_id"), "task_id": r.get("task_id"),
                    "paused_at": _iso(r.get("updated_at")), "created_at": _iso(r.get("created_at")),
                    "handed_back": bool((err or {}).get("handed_back"))})
    return out


def paused_count(connection: Any) -> int:
    try:
        return int(connection.execute(text(
            "SELECT count(*) FROM jobs WHERE status IN ('queued', 'failed_retryable') "
            "AND last_error->>'code' = :code"), {"code": REASON}).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def status(connection: Any) -> dict[str, Any]:
    """HoldStatus, as docs/infra/INFRA_AGENT_API.md §3 defines it."""
    s = state(connection, fresh=True)
    return {"active": bool(s.get("active")), "incident_id": s.get("incident_id"),
            "host_id": s.get("host_id"), "since": s.get("since") if s.get("active") else None,
            "scope": s.get("scope") or "all",
            "exempt_kinds": list(s.get("exempt_kinds") or DEFAULT_EXEMPT_KINDS),
            "actor": s.get("actor") or "", "paused_runs": paused_count(connection),
            "released_at": s.get("released_at")}


# ── what a claimer stamps on a job it leaves queued ──────────────────────────────────────────

def hold_message(incident_id: Optional[str]) -> str:
    return (f"Paused by the Infra Agent (incident {incident_id or '—'}): storage is being "
            "cleaned; resumes from its checkpoint")


def stamp_queued_job(connection: Any, job_id: str, hold: Mapping[str, Any]) -> None:
    """claim_job leaves a non-exempt job queued and says why — once per hold, no attempt spent."""
    connection.execute(text("""
        UPDATE jobs SET last_error = CAST(:e AS JSONB), updated_at = now()
         WHERE id = :id AND COALESCE(last_error->>'code', '') <> :code"""),
        {"id": job_id, "code": REASON,
         "e": json.dumps({"code": REASON, "deferred_reason": REASON,
                          "message": hold_message(hold.get("incident_id")),
                          "incident_id": hold.get("incident_id"),
                          "paused_at": _iso(_now())})})


def hand_back(connection: Any, jobs: Any, *, worker_id: str, job_id: str, lease_token: str,
              run_id: Optional[str] = None, run_table: Optional[str] = None) -> dict[str, Any]:
    """A running step reached a checkpoint under the hold: hand its job back with no attempt
    spent (`jobs.defer_leased`), mark the run `paused_by='infra_agent'`."""
    hold = state(connection, fresh=True)
    out = jobs.defer_leased(connection, worker_id, job_id, lease_token,
                            delay_seconds=HANDED_BACK_DELAY_SECONDS, reason=REASON,
                            message=hold_message(hold.get("incident_id")))
    connection.execute(text("""
        UPDATE jobs SET last_error = COALESCE(last_error, '{}'::jsonb)
               || jsonb_build_object('incident_id', CAST(:i AS text), 'handed_back', true,
                                     'paused_at', CAST(:at AS text)) WHERE id = :id"""),
        {"id": job_id, "i": hold.get("incident_id"), "at": _iso(_now())})
    if run_id and run_table in ("code_task_runs", "agent_runs"):
        connection.execute(text(
            f"UPDATE {run_table} SET finalize_ctx = COALESCE(finalize_ctx, '{{}}'::jsonb) "
            "|| jsonb_build_object('paused_by', CAST(:by AS text), 'paused_incident', "
            "CAST(:i AS text), 'paused_at', CAST(:at AS text)) WHERE id = :r"),
            {"by": PAUSED_BY, "i": hold.get("incident_id"), "at": _iso(_now()), "r": run_id})
    return {**out, "handed_back": True, "incident_id": hold.get("incident_id")}


class PauseAtCheckpoint(Exception):
    """Raised by a running build at a stage boundary under the hold: the stage's work is saved
    (its checkpoint and completed_stages marker are already written); the job is handed back."""

    def __init__(self, stage: str = "") -> None:
        super().__init__(f"paused by the Infra Agent after stage {stage or '?'}")
        self.stage = stage


def active_for_run(engine: Any, kind: str, run_id: Optional[str] = None) -> bool:
    """Is the hold on for THIS running job? For a worker that holds no connection of its own
    (the kernel's drain hook, a build's stage boundary). Cached like `active`."""
    try:
        with engine.connect() as c:
            hold = state(c)
            if not hold.get("active"):
                return False
            payload = {"run_id": run_id} if run_id else {}
            return not exempt_job(c, {"kind": kind, "payload": payload}, hold)
    except Exception:  # noqa: BLE001 — an unreadable state never pauses anyone
        return False


def drain_or_hold(engine: Any, drain: Any, kind: str, run_id: Optional[str]) -> Any:
    """The kernel's `drain` hook with the hold folded in: a run ends at its next step boundary
    ("drained") when BOSS is draining OR the hold applies to it. `_settle` tells the two apart."""
    def _check() -> bool:
        try:
            if drain is not None and drain():
                return True
        except Exception:  # noqa: BLE001
            pass
        return active_for_run(engine, kind, run_id)

    return _check


def hand_back_run(engine: Any, jobs: Any, run_id: str, project_id: Optional[str],
                  fencing_token: Optional[str], *, stage: str = "") -> dict[str, Any]:
    """A code-task build reached a stage boundary under the hold: give its leased job back (no
    attempt spent) and mark the run paused_by='infra_agent'. Mirrors drain.hand_back_interrupted;
    the resume path (finalize_ctx.completed_stages) is the one a deploy already exercises."""
    import hashlib

    out = {"status": "running", "done": False, "job_deferred": True,
           "error": "paused by the Infra Agent while storage is cleaned; it resumes from "
                    "its last completed stage"}
    try:
        with engine.begin() as c:
            hold = state(c, fresh=True)
            c.execute(text(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                "|| jsonb_build_object('paused_by', CAST(:by AS text), 'paused_incident', "
                "CAST(:i AS text), 'paused_at', CAST(:t AS text), 'paused_after_stage', "
                "CAST(:s AS text)) WHERE id = :id"),
                {"by": PAUSED_BY, "i": hold.get("incident_id"), "t": _iso(_now()), "s": stage,
                 "id": run_id})
            if fencing_token and hasattr(jobs, "defer_leased"):
                job = c.execute(text(
                    "SELECT id, lease_owner FROM jobs WHERE kind = 'code.task' "
                    "AND payload->>'run_id' = :r AND lease_token_hash = :h "
                    "AND status IN ('leased', 'running') LIMIT 1"),
                    {"r": run_id,
                     "h": hashlib.sha256(fencing_token.encode("utf-8")).hexdigest()},
                ).mappings().first()
                if job is not None:
                    hand_back(c, jobs, worker_id=job["lease_owner"], job_id=job["id"],
                              lease_token=fencing_token)
                    out["job_id"] = job["id"]
            _emit(c, project_id, "code_task.paused_by_infra_agent", "agent:infra", run_id,
                  {"run_id": run_id, "stage": stage, "incident_id": hold.get("incident_id")})
    except Exception:  # noqa: BLE001 — the lease expires and is reclaimed either way
        _log.warning("could not hand back the job of %s under the hold", run_id, exc_info=True)
    return out


def _emit(connection: Any, project_id: Optional[str], event_type: str, actor: str,
          entity_id: Optional[str], data: dict[str, Any]) -> None:
    try:
        from .core import emit_event

        pid = project_id or _default_project(connection)
        if pid:
            emit_event(connection, pid, event_type, actor, "storage_incident",
                       str(entity_id or "install"), data)
    except Exception:  # noqa: BLE001 — an event is a note, never a reason to hold the hold
        _log.debug("event %s not emitted", event_type, exc_info=True)


def _default_project(connection: Any) -> Optional[str]:
    try:
        return connection.execute(text(
            "SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
    except Exception:  # noqa: BLE001
        return None


__all__ = ["DEFAULT_EXEMPT_KINDS", "KEY", "PAUSED_BY", "PauseAtCheckpoint", "REASON", "active",
           "active_for_run", "drain_or_hold", "exempt_job", "exempt_kind", "hand_back",
           "hand_back_run", "hold_message", "invalidate_cache", "paused_count", "paused_jobs",
           "release", "set_hold", "stamp_queued_job", "state", "status"]
