"""A BUILD A BOSS BUG STOPPED IS RESTARTED BY BOSS ONCE BOSS IS UPDATED — NEVER LEFT RED.

THE CASE (laptop, 2026-11-18). CSV Quality Desk's Easy Mode build (crun_3c87d5a3) died at 07:51
on a BOSS bug: it was started against the placeholder repository `_local/lucky`, which nothing
can clone ("The control plane crashed running this step (HTTP 500): GitHubAppConfigurationError:
No GitHub credentials for _local/lucky"). PR #203 fixed the bug the same morning and the laptop
was upgraded — but the build stayed failed, red, with "Read the failure and press Retry": the
operator saw "failed again and there's no one to fix it".

THE RULE (`sweep`, on the monitor tick):
  * WHICH BUILDS: failed in the last REACH_HOURS on a PLATFORM cause (failure_class `infra`, or a
    control-plane crash the pipeline never classified), the newest attempt of its lineage (no
    attempt continues it), not a workflow run's (its step's own recovery owns it), not merged —
    and BOSS has been UPDATED since it failed (a worker start on a different commit,
    install_history's boss_starts). A mere restart on the same code is not a reason: the same
    code fails the same way.
  * ONCE: the build is claimed (`finalize_ctx.restart_recovery`) before anything is tried, so no
    tick, no second worker and no later update tries it again.
  * HOW: what the fix changed about how a build starts is redone first — a build started against
    the Easy Mode placeholder repository gets the project's real local repository, exactly as
    Easy Mode makes one now (projects.ensure_sample_code) — then BOSS retries it as itself,
    under the grant the person gave it (retry_code_task(automatic=True, after_update=True)),
    resuming its saved work.
  * SAID IN WORDS, NEVER RED: restarted → "Stopped by a BOSS bug fixed in this update — BOSS
    restarted it"; not possible (no grant of the person's covers the new attempt, the repository
    could not be made) → "Stopped by a BOSS bug fixed in this update — BOSS can't resume it by
    itself: <why>. Start it again." The failed-build card reads that (build_failure).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

#: How far back a stopped build is still worth restarting by itself.
REACH_HOURS = 72
#: Builds restarted per tick (each is a real build).
MAX_PER_TICK = 3
#: The finalize_ctx key that claims a build for this recovery, once.
KEY = "restart_recovery"
#: The Easy Mode placeholder repository builds were started against before PR #203.
PLACEHOLDER = ("_local", "lucky")
#: A crash of the control plane the pipeline never got to classify (no failure_class).
_CRASH = "The control plane crashed%"

RESTARTED_WORDS = "Stopped by a BOSS bug fixed in this update — BOSS restarted it."
CANNOT_WORDS = ("Stopped by a BOSS bug fixed in this update — BOSS can't resume it by itself: "
                "{why} Start it again.")

_CANDIDATES = (
    "SELECT c.id, c.project_id, c.updated_at, c.spec FROM code_task_runs c "
    "WHERE c.status = 'failed' AND c.updated_at > :since AND c.updated_at < :started "
    "AND c.finalize_ctx->'" + KEY + "' IS NULL "
    "AND c.finalize_ctx->>'workflow_run_id' IS NULL "
    "AND c.finalize_ctx->'merge'->>'sha' IS NULL "
    "AND (c.finalize_ctx->>'failure_class' = 'infra' "
    "     OR (c.finalize_ctx->>'failure_class' IS NULL AND c.error LIKE :crash)) "
    "AND NOT EXISTS (SELECT 1 FROM code_task_runs n WHERE n.project_id = c.project_id "
    "  AND n.finalize_ctx->>'parent_run_id' = c.id) "
    "ORDER BY c.updated_at DESC LIMIT :n")


def _aware(v: Any) -> Optional[datetime]:
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def updated_since(connection: Any, failed_at: Any) -> bool:
    """BOSS's worker has started on a DIFFERENT commit since `failed_at` (an upgrade)."""
    try:
        with connection.begin_nested():
            then = connection.execute(text(
                "SELECT source_commit FROM boss_starts WHERE service = 'worker' "
                "AND started_at <= :t ORDER BY started_at DESC LIMIT 1"), {"t": failed_at}).scalar()
            if not then:
                return False
            return connection.execute(text(
                "SELECT 1 FROM boss_starts WHERE service = 'worker' AND started_at > :t "
                "AND source_commit IS NOT NULL AND source_commit <> :c LIMIT 1"),
                {"t": failed_at, "c": then}).first() is not None
    except Exception:  # noqa: BLE001 — no install record: never a reason to retry
        return False


def _claim(connection: Any, run_id: str, now: datetime) -> bool:
    import json

    return connection.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
        "jsonb_build_object(CAST(:k AS text), CAST(:v AS jsonb)) "
        "WHERE id = :r AND status = 'failed' AND finalize_ctx->'" + KEY + "' IS NULL "
        "RETURNING id"), {"k": KEY, "r": run_id,
                          "v": json.dumps({"state": "trying", "at": now.isoformat()})}
    ).first() is not None


def _record(connection: Any, run_id: str, outcome: Mapping[str, Any]) -> None:
    import json

    connection.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
        "jsonb_build_object(CAST(:k AS text), CAST(:v AS jsonb)) WHERE id = :r"),
        {"k": KEY, "r": run_id, "v": json.dumps(dict(outcome))})


def repair_placeholder(connection: Any, settings: Any, run: Mapping[str, Any]) -> Optional[str]:
    """A build started against the Easy Mode placeholder repository gets the project's real local
    repository (made now when it has none, the way Easy Mode makes one). Returns the repository
    it now builds in, None when it was not a placeholder build; raises ValueError when the
    project can get no repository."""
    import json

    spec = run.get("spec") or {}
    if isinstance(spec, str):
        spec = json.loads(spec)
    if (str(spec.get("owner") or ""), str(spec.get("repo") or "")) != PLACEHOLDER:
        return None
    from . import local_projects
    from .projects import ensure_sample_code

    pid = str(run["project_id"])
    bound = local_projects.local_binding(connection, pid)
    if bound is None:
        ensure_sample_code(connection, settings, pid, actor="boss")
        bound = local_projects.local_binding(connection, pid)
    full = str((bound or {}).get("full_name") or "")
    owner, _, repo = full.partition("/")
    if not owner or not repo:
        raise ValueError("this project has no repository to build in, and BOSS could not make "
                         "one on this machine.")
    connection.execute(text(
        "UPDATE code_task_runs SET spec = COALESCE(spec, '{}'::jsonb) || jsonb_build_object("
        "'owner', CAST(:o AS text), 'repo', CAST(:r AS text), 'repo_host', 'local') "
        "WHERE id = :id"), {"o": owner, "r": repo, "id": run["id"]})
    return full


def sweep(engine: Any, settings: Any, jobs: Any = None, *,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Restart, once, each build a BOSS bug stopped before BOSS was updated. Never raises."""
    from .code_task_service import PROCESS_STARTED_AT, RetryNeedsPerson, retry_code_task
    from .core import emit_event

    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"restarted": [], "cannot_resume": []}
    try:
        with engine.begin() as c:
            rows = [dict(r) for r in c.execute(text(_CANDIDATES), {
                "since": now - timedelta(hours=REACH_HOURS), "started": PROCESS_STARTED_AT,
                "crash": _CRASH, "n": MAX_PER_TICK * 4}).mappings()]
            rows = [r for r in rows if updated_since(c, r["updated_at"])][:MAX_PER_TICK]
            rows = [r for r in rows if _claim(c, str(r["id"]), now)]
    except Exception:  # noqa: BLE001 — a sweep never fails its tick
        log.warning("restart recovery could not read its builds", exc_info=True)
        return out
    if jobs is None:
        from .jobs import JobLeaseService

        jobs = JobLeaseService(settings)
    for row in rows:
        run_id, pid = str(row["id"]), str(row["project_id"])
        repo = None
        try:
            with engine.begin() as c:
                repo = repair_placeholder(c, settings, row)
            got = retry_code_task(engine, settings, jobs, run_id, automatic=True,
                                  platform_retry=True, after_update=True)
            new_id = str(got.get("run_id") or "")
            with engine.begin() as c:
                _record(c, run_id, {"state": "restarted", "at": now.isoformat(),
                                    "run_id": new_id, "words": RESTARTED_WORDS,
                                    **({"repository": repo} if repo else {})})
                emit_event(c, pid, "code_task.restarted_after_update", "boss", "code_task_run",
                           run_id, {"retry_run_id": new_id, "words": RESTARTED_WORDS,
                                    **({"repository": repo} if repo else {})})
            out["restarted"].append(run_id)
        except Exception as exc:  # noqa: BLE001 — RetryNeedsPerson, a refusal, a crash
            why = _why(exc, RetryNeedsPerson)
            words = CANNOT_WORDS.format(why=why)
            try:
                with engine.begin() as c:
                    _record(c, run_id, {"state": "cannot_resume", "at": now.isoformat(),
                                        "words": words, **({"repository": repo} if repo else {})})
                    emit_event(c, pid, "code_task.restart_after_update_withheld", "boss",
                               "code_task_run", run_id, {"words": words})
            except Exception:  # noqa: BLE001
                log.warning("restart recovery could not record %s", run_id, exc_info=True)
            out["cannot_resume"].append(run_id)
    return out


def _why(exc: BaseException, needs_person: type) -> str:
    if isinstance(exc, needs_person):
        return ("no grant you gave covers another attempt on its repository, so it needs your "
                "go-ahead.")
    if isinstance(exc, ValueError) and "repository" in str(exc):
        return str(exc)
    return "it could not be started again automatically."


def words_for(fctx: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """What the failed-build card says for a build this recovery handled, or None."""
    rec = (fctx or {}).get(KEY)
    if not isinstance(rec, Mapping) or rec.get("state") not in ("restarted", "cannot_resume"):
        return None
    return {"state": str(rec["state"]), "words": str(rec.get("words") or "")}
