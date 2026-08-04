"""The missing link: a merged PR becomes a deployment.

Design: docs/product/CICD_DEPLOYMENT_PLAN.md

BOSS already has the whole delivery apparatus — `resource_plane.plan_deployment_run`,
`deployment_rollout_engine`, `promotion_orchestrator`, `telemetry_connectors`, and nine
tables behind them. Every one of those tables was EMPTY, and nothing in the code-task or
merge path referenced any of it. The parts existed and had never been assembled.

This module is only the seam. It deliberately builds no new deployment machinery: it observes
a merge, resolves the target, and calls `plan_deployment_run`. Writing a fourth way to insert
a `deployment_runs` row is exactly the "two copies of one decision" defect that produced the
preview-resolver, test-oracle and failure-taxonomy bugs in this codebase already.

Boundaries, each carried from something already paid for:

  * STAGING IS AUTOMATIC, PRODUCTION IS A HUMAN GATE. An agent promoting its own build to
    production is what a governance product exists to prevent — the same reasoning that makes
    a Tier-2 merge approval require the `ceo` role.
  * A deployment_run row exists BEFORE anything is attempted, so a crash leaves a record. The
    code-task pipeline learned this by losing whole runs to state written only at the end.
  * Best-effort at the seam: a delivery failure must never retroactively un-merge a PR or
    fail a completed code task. It is recorded and surfaced, not raised.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import emit_event, make_id

# Environments a merge may deploy to WITHOUT a human. Everything else needs a promotion
# decision. Deliberately a set rather than a flag: adding "production" here should be a
# visible, reviewable change, not a config toggle.
AUTO_DEPLOY_ENVIRONMENTS = frozenset({"staging", "dev", "preview"})


def target_for(connection, project_id: str, repo: str, environment: str) -> Optional[dict]:
    """The deployment target for this repo+environment, or None if none is registered.

    Returning None is the normal case for a project that has not opted into delivery, and it
    must stay silent: a repo with no target is not a failure, it is a repo nobody asked to
    deploy.
    """
    row = connection.execute(
        sql("SELECT id, app_name, environment, repo, target_kind, target_ref, "
            "       health_checks, rollback_points, status "
            "FROM deployment_targets "
            "WHERE project_id = :p AND repo = :r AND environment = :e "
            "  AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "r": repo, "e": environment},
    ).mappings().first()
    return dict(row) if row else None


def on_pull_request_merged(engine, settings: Settings, *, project_id: str, run_id: str,
                           repo: str, commit_ref: Optional[str], decision_id: Optional[str],
                           actor: str = "delivery") -> dict[str, Any]:
    """Called when a merge decision completes. Starts a staging deployment if one is configured.

    Returns a dict describing what happened, always — `skipped` is a first-class outcome and
    the common one. Never raises: this runs after a PR has already merged, and an exception
    here would turn a successful delivery into a failure report for work that shipped.
    """
    from ..db import begin

    result: dict[str, Any] = {"status": "skipped", "reason": "no active staging target"}
    try:
        with begin(engine) as c:
            target = target_for(c, project_id, repo, "staging")
            if not target:
                # Silent by design. Recorded at debug level via the event so the absence is
                # visible when someone asks "why did nothing deploy?", without alarming a
                # project that never wanted delivery.
                emit_event(c, project_id, "delivery.skipped", actor, "code_task_run", run_id,
                           {"why": "no active staging deployment_target for this repo",
                            "repo": repo})
                return result

            if target["environment"] not in AUTO_DEPLOY_ENVIRONMENTS:
                emit_event(c, project_id, "delivery.gated", actor, "code_task_run", run_id,
                           {"why": f"{target['environment']} requires a promotion decision",
                            "target": target["id"]})
                return {"status": "gated", "environment": target["environment"],
                        "target_id": target["id"]}

            from .resource_plane import ResourcePlaneService

            plane = ResourcePlaneService(settings)
            planned = plane.plan_deployment_run(
                c,
                project_id=project_id,
                deployment_target_id=target["id"],
                commit_ref=commit_ref,
                release_packet={
                    "source": "code_task_merge",
                    "code_task_run_id": run_id,
                    "decision_id": decision_id,
                    "repo": repo,
                },
                # The merge is already paid for; the deployment itself is not an LLM cost.
                expected_cost_usd_micros=0,
                reservation_id=None,
                actor=actor,
            )
            emit_event(c, project_id, "delivery.deployment_planned", actor,
                       "code_task_run", run_id,
                       {"deployment_run_id": planned.get("deployment_run_id")
                        or planned.get("id"),
                        "environment": target["environment"], "target": target["id"],
                        "commit_ref": commit_ref})
            result = {"status": "planned", "environment": target["environment"],
                      "target_id": target["id"],
                      "deployment_run_id": planned.get("deployment_run_id")
                      or planned.get("id")}
    except Exception as exc:  # noqa: BLE001
        # A delivery failure must never un-merge a PR or fail a completed code task.
        try:
            with begin(engine) as c:
                emit_event(c, project_id, "delivery.failed", actor, "code_task_run", run_id,
                           {"error": f"{type(exc).__name__}: {exc}"[:300], "repo": repo})
        except Exception:  # noqa: BLE001
            pass
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
    return result


def pipeline_view(connection, project_id: str, *, limit: int = 25) -> dict[str, Any]:
    """One query pass for the console: code task -> PR -> deployment -> rollout -> health.

    Assembled here rather than in the UI so the stages cannot disagree with each other — the
    same reason the shepherd console has a single overview endpoint.
    """
    runs = connection.execute(
        sql("""
            SELECT r.id AS run_id, r.title, r.status AS run_status,
                   r.pull_request_url, r.pull_request_number, r.updated_at,
                   d.id AS deployment_run_id, d.status AS deploy_status,
                   d.environment, d.app_name, d.target_kind, d.commit_ref,
                   d.started_at, d.finished_at, d.failure_reason,
                   d.health_check_results
            FROM code_task_runs r
            LEFT JOIN deployment_runs d
                   ON (d.release_packet->>'code_task_run_id') = r.id
            WHERE r.project_id = :p AND r.pull_request_url IS NOT NULL
            ORDER BY r.updated_at DESC LIMIT :l
        """), {"p": project_id, "l": limit}).mappings().all()

    targets = connection.execute(
        sql("SELECT id, app_name, environment, repo, target_kind, target_ref, status "
            "FROM deployment_targets WHERE project_id = :p ORDER BY environment"),
        {"p": project_id}).mappings().all()

    return {
        "project_id": project_id,
        "stages": [dict(r) for r in runs],
        "targets": [dict(t) for t in targets],
        # Stated plainly so the console can say "nothing is configured" rather than
        # rendering an empty table that looks like a failure.
        "configured": bool(targets),
    }
