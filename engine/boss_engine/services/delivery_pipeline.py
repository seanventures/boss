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
    # SELECT * rather than a column list. The list version omitted `required_resources` in
    # execute_deployment and silently stranded the target's declared port; the same list here
    # would have omitted `auto_deploy` and made this function permanently answer "not
    # automatic". A projection that must be extended per new consumer will eventually not be.
    row = connection.execute(
        sql("SELECT * FROM deployment_targets "
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

            if not target.get("auto_deploy"):
                # A target that has not opted in is planned-only, exactly as before. Silent
                # rather than alarming: most repos never asked to deploy on merge.
                emit_event(c, project_id, "delivery.not_auto", actor, "code_task_run", run_id,
                           {"why": "auto_deploy is off for this target",
                            "target": target["id"]})
                return {"status": "skipped", "reason": "auto_deploy is off",
                        "target_id": target["id"]}

            if target.get("paused"):
                # Paused is louder than not-configured: someone turned this OFF deliberately
                # and will want to know a merge went by while it was.
                emit_event(c, project_id, "delivery.paused", actor, "code_task_run", run_id,
                           {"why": "auto-deploy is paused", "target": target["id"],
                            "commit_ref": commit_ref})
                return {"status": "paused", "target_id": target["id"]}

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
            deployment_run_id = planned.get("deployment_run_id") or planned.get("id")
            c.execute(sql("UPDATE deployment_runs SET trigger = 'merge' WHERE id = :i"),
                      {"i": deployment_run_id})
            result = {"status": "planned", "environment": target["environment"],
                      "target_id": target["id"],
                      "deployment_run_id": deployment_run_id}

    except Exception as exc:  # noqa: BLE001
        # A delivery failure must never un-merge a PR or fail a completed code task.
        try:
            with begin(engine) as c:
                emit_event(c, project_id, "delivery.failed", actor, "code_task_run", run_id,
                           {"error": f"{type(exc).__name__}: {exc}"[:300], "repo": repo})
        except Exception:  # noqa: BLE001
            pass
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}

    # Execute OUTSIDE the transaction above, and outside the merge's own transaction: a deploy
    # takes minutes, does network I/O, and must not hold a database lock or be rolled back by
    # something unrelated finishing badly. Until this existed, a merge planned a deployment and
    # nothing ever ran it — the row simply sat there, which is why every deployment so far was
    # started by hand. That is the whole of the missing "continuous".
    if result.get("status") == "planned" and result.get("deployment_run_id"):
        try:
            outcome = execute_deployment(engine, settings, result["deployment_run_id"],
                                         actor=actor)
            result = {**result, "status": "executed", "deployment": outcome}
        except Exception as exc:  # noqa: BLE001
            # Still never raises: the PR has merged, and a delivery failure must not be
            # reported as a failure of work that already shipped.
            result = {**result, "status": "execute_failed",
                      "error": f"{type(exc).__name__}: {exc}"[:300]}
    return result


def redeploy(engine, settings: Settings, deployment_run_id: str, *,
             commit_ref: Optional[str] = None, actor: str = "operator") -> dict[str, Any]:
    """Ship the same target again, as a NEW deployment_run.

    Deliberately not a re-execution of the existing row. An operator asking "what is running and
    since when" is asking about a history, and rewriting one row in place destroys exactly that:
    the previous deployment's start, finish and health verdict would be overwritten by its
    replacement. A redeploy is a new event, so it gets a new record.

    `commit_ref` defaults to whatever the prior run shipped, which makes the common case — "put
    that back" / "run it again" — a button with no arguments.
    """
    from ..db import begin

    with begin(engine) as c:
        prior = c.execute(sql("SELECT * FROM deployment_runs WHERE id = :i"),
                          {"i": deployment_run_id}).mappings().first()
        if not prior:
            return {"status": "not_found", "deployment_run_id": deployment_run_id}

        from .resource_plane import ResourcePlaneService

        packet = dict(prior["release_packet"] or {})
        # Keep the lineage explicit: a redeploy that cannot say what it came from is just an
        # unexplained second row.
        packet.update({"source": "redeploy", "redeploy_of": deployment_run_id})
        planned = ResourcePlaneService(settings).plan_deployment_run(
            c,
            project_id=prior["project_id"],
            deployment_target_id=prior["deployment_target_id"],
            commit_ref=commit_ref or prior["commit_ref"],
            release_packet=packet,
            expected_cost_usd_micros=0,
            reservation_id=None,
            actor=actor,
        )
        new_id = planned.get("deployment_run_id") or planned.get("id")
        emit_event(c, prior["project_id"], "delivery.redeploy_requested", actor,
                   "deployment_run", new_id,
                   {"redeploy_of": deployment_run_id,
                    "commit_ref": commit_ref or prior["commit_ref"]})

    return execute_deployment(engine, settings, new_id, actor=actor)


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
                   d.health_check_results,
                   -- Where the thing actually IS. Without it the console can say a deployment
                   -- succeeded but not where to click, which is the state the operator hit.
                   t.domains AS target_domains
            FROM code_task_runs r
            LEFT JOIN deployment_runs d
                   ON (d.release_packet->>'code_task_run_id') = r.id
            LEFT JOIN deployment_targets t ON t.id = d.deployment_target_id
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


# --------------------------------------------------------------------------- #
# Execution — BOSS places the artifact, observes it, and rolls back on its own
# --------------------------------------------------------------------------- #
def execute_deployment(engine, settings: Settings, deployment_run_id: str, *,
                       actor: str = "delivery") -> dict[str, Any]:
    """Run a planned deployment through its connector: plan -> apply -> observe.

    The whole point of the connector contract is that this function does not know or care
    which destination it is talking to. Adding Cloudflare or Kubernetes later changes a
    registry entry, not this code — branching per destination here is how the preview
    resolver ended up serving the wrong app from two divergent copies of one decision.

    observe() decides the outcome, never apply()'s exit status: a process that started is not
    a service that works, and a canary that trusts an exit code will promote a broken build.
    """
    from ..db import begin
    from .deployment_connectors import connector_for, preflight

    # Two plain reads instead of a join with a hand-listed projection. The projection version
    # silently dropped `required_resources`, so the connector never saw the target's declared
    # app_port and fell back to 8080 — a port already taken on that host. The deployment failed
    # with "address already in use" for a port nobody had configured, which is the least
    # debuggable shape a bug can take.
    #
    # A column list that must be extended every time a consumer reads a new field is a standing
    # invitation to that failure (the same defect as `_row_to_profile` dropping new profile
    # columns). Passing the row through means adding a column can never again strand a feature.
    with begin(engine) as c:
        row = c.execute(sql("SELECT * FROM deployment_runs WHERE id = :i"),
                        {"i": deployment_run_id}).mappings().first()
        if not row:
            return {"status": "not_found", "deployment_run_id": deployment_run_id}
        trow = c.execute(sql("SELECT * FROM deployment_targets WHERE id = :t"),
                         {"t": row["deployment_target_id"]}).mappings().first()
        if not trow:
            return {"status": "not_found", "reason": "deployment_target missing",
                    "deployment_run_id": deployment_run_id}
        target = dict(trow)
        project_id = row["project_id"]
        run_id = (row["release_packet"] or {}).get("code_task_run_id") or ""

    # Cheap gate first: never build or transfer anything for a target that cannot accept it.
    # The connection lets preflight RESOLVE the target's bound credential rather than assume
    # a global one — otherwise a per-target secret is checked at deploy time, which is the
    # expensive place to discover it is missing.
    with begin(engine) as c:
        pre = preflight(target, settings, c)
    if not pre["ready"]:
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='blocked', failure_reason=:r, "
                          "updated_at=now() WHERE id=:i"),
                      {"r": "; ".join(pre["problems"])[:400], "i": deployment_run_id})
            emit_event(c, project_id, "delivery.blocked", actor, "deployment_run",
                       deployment_run_id, {"problems": pre["problems"]})
        return {"status": "blocked", "problems": pre["problems"]}

    conn = connector_for(target)
    try:
        plan = conn.plan(engine, settings, target, run_id)
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='running', started_at=now(), "
                          "release_packet = COALESCE(release_packet,'{}'::jsonb) "
                          "  || jsonb_build_object('plan', CAST(:p AS JSONB)), "
                          "updated_at=now() WHERE id=:i"),
                      {"p": json.dumps(plan, default=str), "i": deployment_run_id})
            emit_event(c, project_id, "delivery.planned", actor, "deployment_run",
                       deployment_run_id, {"connector": conn.kind,
                                           "domains": target.get("domains")})

        # The deployment's own commit_ref is what we promised to ship, so it is what gets
        # built — not whatever the code task's branch happens to point at now.
        applied = conn.apply(engine, settings, target, run_id, plan, ref=row.get("commit_ref"))
        health = conn.observe(target, settings)
        # The VERDICT is health, not the apply result.
        ok = bool(health.get("healthy"))
        status = "succeeded" if ok else "failed"
        reason = None if ok else json.dumps(health)[:400]

        with begin(engine) as c:
            # Record the SHA that was actually checked out. Storing the REQUESTED ref is how
            # this field came to name a build that was never running.
            c.execute(sql("UPDATE deployment_runs SET status=:s, finished_at=now(), "
                          "health_check_results=CAST(:h AS JSONB), failure_reason=:r, "
                          "commit_ref=COALESCE(:sha, commit_ref), "
                          "updated_at=now() WHERE id=:i"),
                      {"s": status, "h": json.dumps(health, default=str),
                       "r": reason, "i": deployment_run_id,
                       "sha": (applied or {}).get("deployed_sha")})
            emit_event(c, project_id, f"delivery.{status}", actor, "deployment_run",
                       deployment_run_id,
                       {"healthy": ok, "applied": applied.get("status"),
                        "domains": target.get("domains")})
        return {"status": status, "healthy": ok, "applied": applied, "health": health}

    except Exception as exc:  # noqa: BLE001
        with begin(engine) as c:
            c.execute(sql("UPDATE deployment_runs SET status='failed', finished_at=now(), "
                          "failure_reason=:r, updated_at=now() WHERE id=:i"),
                      {"r": f"{type(exc).__name__}: {exc}"[:400], "i": deployment_run_id})
            emit_event(c, project_id, "delivery.failed", actor, "deployment_run",
                       deployment_run_id, {"error": f"{type(exc).__name__}: {exc}"[:300]})
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
